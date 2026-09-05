# AI Revenue Recovery

A live operations console that finds leaking revenue and recovers it, across two channels that share one spine — **detect → root cause → recover**:

- **Live payments** — detects payment-success-rate degradation in real time, explains the root cause, and takes a gateway-level recovery action (reroute / retry / circuit-break).
- **Recurring revenue** — recovers failed subscription renewals with a smart-retry agent and AI-generated dunning messages tuned to *why* each renewal failed.

Built for the Razorpay AI Buildathon (AI Revenue Recovery track).

## Why this shape

Revenue leaks in two very different places — a flaky acquirer route bleeding one-off payments, and failed auto-renewals bleeding MRR. Both are the same problem: *detect the leak, explain it, act on it.* This project treats them as one platform with two channels over a shared engine, so the intelligence (statistical detection, root-cause localisation, an LLM reasoning layer with a deterministic fallback) is written once.

## Quickstart

```bash
pip install -r requirements.txt
python run.py
# open http://localhost:8000
```

No API keys, no database, no build step, no Node. One Python process. The app runs fully on a deterministic rule-based reasoner out of the box (shown as `LLM: rules` in the top bar).

Alternatively: `make install && make run`, or `docker compose up --build`.

### Optional: plug in an LLM

Copy `.env.example` to `.env` and set a provider + key to have an LLM write the root-cause narratives and dunning messages instead of the built-in rules:

```bash
LLM_PROVIDER=anthropic          # anthropic | openai | gemini | none
ANTHROPIC_API_KEY=sk-ant-...
```

If the key is missing or a call fails, it silently falls back to the rule-based reasoner — the demo never breaks.

## Try it (90-second demo)

1. Open the dashboard. The **Live payments** tab shows healthy routes and the money band up top.
2. Hit **Simulate gateway timeout** (or **Simulate bank declines**). Within a few seconds a route drops, the **Diagnosis** panel fires: an anomaly tag with a z-score, a root-cause narrative, a confidence bar, and a recommended action.
3. A localised gateway fault recommends **Reroute to a healthy gateway**; a bank-side decline recommends **Circuit-break + alert** instead. Click **Apply recovery** — the route heals, the **Recovered** number counts up, and the anomaly resolves.
4. Switch to **Recurring revenue** and hit **Run recovery agent** — 24 failed renewals get a per-reason recovery plan (retry timing, channel, message) and a projected recovered-MRR funnel.

## How it works

      ┌──────────────┐   transactions   ┌───────────────┐
      │  simulators  │ ───────────────▶ │    monitor    │  rolling window,
      │ payments/    │                   │  (detect)     │  per-route baseline,
      │ subscriptions│                   └──────┬────────┘  2-proportion z-test
      └──────────────┘                          │ anomaly
                                                 ▼
                          ┌───────────────┐   evidence   ┌──────────────┐
                          │  root cause   │ ───────────▶ │  LLM client  │  narrative
                          │  (localise)   │              │  + fallback  │  (or rules)
                          └──────┬────────┘              └──────────────┘
                                 │ evidence
                                 ▼
                          ┌───────────────┐    action    ┌──────────────┐
                          │    policy     │ ───────────▶ │    engine    │  applies action,
                          │  (recover)    │              │  (SSE state) │  accrues recovery
                          └───────────────┘              └──────────────┘

                          
**Detection** runs at the route grain (`method/issuer`) using a slow per-route EWMA baseline that only learns while a route is healthy (so an ongoing incident is never normalised away), and fires on a two-proportion z-test combined with an absolute-drop floor and a minimum sample size.

**Root cause** localises the fault: it compares the implicated acquirer's success rate against the same route on other acquirers. A big gap ⇒ a localised gateway fault (reroutable); no gap with issuer decline codes ⇒ a bank-side problem (rerouting won't help).

**Recovery policy** is deterministic and safe — the LLM writes the human explanation, it never picks the action:

| Fault | Signal | Action |
|---|---|---|
| Gateway, localised to one acquirer | timeouts/errors on one gateway, others healthy | **Reroute** to the healthiest alternate |
| Gateway, not localised | transient errors, no clean alternate | **Retry** with backoff + idempotency |
| Bank / issuer decline | decline codes across all acquirers | **Circuit-break** + alert, surface alt method |
| Customer / risk | invalid instrument, risk block | **Alert only** — not auto-recoverable |

**Subscription recovery** picks an action per failure reason — insufficient funds → smart retry near a likely credit date; expired card → never blind-retry, drive a card update; do-not-honor → retry once then escalate; technical decline → immediate silent retry — estimates a recovery probability per account, and projects recovered MRR.

## Stopping rules & compliant escalation

Neither recovery loop runs forever:

- **Subscriptions**: after `MAX_AUTO_ATTEMPTS` (3) failed renewal attempts, an account stops being auto-retried and stops being auto-contacted — it's escalated to a human instead (`ESCALATE_TO_HUMAN`, one message explaining a person will follow up, then silence). This bounds both wasted retries and repeated customer contact.
- **Payments**: an applied recovery action (reroute/retry) that hasn't healed the segment within `MAX_ACTION_TICKS` (25) ticks automatically escalates — the automated action stops, the incident is cleared, and it's logged as needing on-call attention instead of retrying indefinitely.
- **Audit trail**: every anomaly, root-cause narrative, applied action, resolution, and escalation is timestamped in the engine's event log (`/api/state` → `events`), so the full detect → recover → (resolve | escalate) lifecycle for any incident is reconstructable after the fact.

## Project layout
app/
config.py settings (env-driven, safe defaults)
models.py domain models + error-code taxonomy
engine.py orchestrates detect→rootcause→recover, holds live state
main.py FastAPI: SSE stream + action endpoints
simulator/
payments.py synthetic payment stream + injectable incidents
subscriptions.py synthetic failed renewals
detect/
monitor.py rolling window, EWMA baselines, z-test detector
rootcause.py gateway localisation / contribution analysis
recover/
policy.py payment recovery policy + uplift projection
subscription_agent.py per-reason retry/dunning playbook + funnel
llm/
client.py pluggable Anthropic/OpenAI/Gemini + rule fallback
static/ no-build dashboard (HTML + CSS + JS, SSE)
tests/test_core.py detection, RCA classification, policy, subscriptions


## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | dashboard |
| GET | `/api/state` | one-shot snapshot |
| GET | `/api/stream` | Server-Sent Events, pushed each tick |
| POST | `/api/incident/trigger?kind=gateway\|bank` | inject a degradation incident |
| POST | `/api/action/apply` | apply the recommended payment action |
| GET | `/api/subscriptions` | subscription recovery state |
| POST | `/api/subscriptions/recover` | run the subscription recovery agent |

## Tests

```bash
make test        # or: python -m pytest -q
```

Covers: no false positive on healthy traffic, a gateway incident is detected and localised, a bank decline is *not* rerouted, a reroute restores success rate, the subscription playbook never blind-retries an expired card, and the stopping rule correctly escalates an account that's hit the max auto-retry attempts.

## Notes & honesty

Data is synthetic and the stream is generated in-process; recovery actions simulate their own uplift so the effect is visible in the demo. The detection, root-cause, and policy logic is real and provider-agnostic — point the simulators at a real event stream and the same spine applies.
