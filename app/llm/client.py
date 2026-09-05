"""Pluggable LLM layer.

Two jobs: (1) turn root-cause evidence into a human narrative, (2) write a dunning
message. Both degrade gracefully: if no provider/key is configured, or the call
fails, we return a deterministic result so the whole app still works offline.

Providers are called over plain HTTP (httpx) so there's no SDK version coupling.
"""
from __future__ import annotations

import json

import httpx

from ..config import settings
from ..models import RootCause, Subscription


class LLMClient:
    def __init__(self):
        self.provider = settings.llm_provider
        self.model = settings.default_model()
        self.key = settings.api_key()

    # -- public API --------------------------------------------------------
    def root_cause(self, evidence: dict) -> RootCause:
        prompt = _rca_prompt(evidence)
        raw = self._complete(prompt, want_json=True)
        if raw:
            try:
                data = json.loads(raw)
                return RootCause(
                    anomaly_id=evidence["segment"],
                    narrative=data["narrative"].strip(),
                    confidence=float(data.get("confidence", 0.7)),
                    source=f"llm:{self.provider}",
                )
            except Exception:
                pass
        return rules_root_cause(evidence)

    def dunning_message(self, sub: Subscription, playbook: dict) -> str:
        prompt = _dunning_prompt(sub, playbook)
        raw = self._complete(prompt, want_json=False)
        return (raw or "").strip()

    # -- transport ---------------------------------------------------------
    def _complete(self, prompt: str, want_json: bool) -> str | None:
        if not settings.llm_enabled:
            return None
        try:
            if self.provider == "anthropic":
                return self._anthropic(prompt)
            if self.provider == "openai":
                return self._openai(prompt, want_json)
            if self.provider == "gemini":
                return self._gemini(prompt)
        except Exception:
            return None
        return None

    def _anthropic(self, prompt: str) -> str:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 400,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=settings.llm_timeout,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []))

    def _openai(self, prompt: str, want_json: bool) -> str:
        body = {"model": self.model, "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}]}
        if want_json:
            body["response_format"] = {"type": "json_object"}
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"authorization": f"Bearer {self.key}", "content-type": "application/json"},
            json=body, timeout=settings.llm_timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _gemini(self, prompt: str) -> str:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent?key={self.key}")
        r = httpx.post(url, headers={"content-type": "application/json"},
                       json={"contents": [{"parts": [{"text": prompt}]}]},
                       timeout=settings.llm_timeout)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]


# -- prompts -----------------------------------------------------------------

def _rca_prompt(e: dict) -> str:
    return (
        "You are a payments reliability engineer. Given this evidence about a payment "
        "success-rate degradation, write a crisp root-cause explanation for an on-call "
        "operator. Be specific and quantitative; do not invent facts beyond the evidence.\n\n"
        f"EVIDENCE (JSON):\n{json.dumps(e, indent=2)}\n\n"
        'Respond ONLY with JSON: {"narrative": "<=60 words", "confidence": 0.0-1.0}. '
        "No markdown, no preamble."
    )


def _dunning_prompt(sub: Subscription, playbook: dict) -> str:
    return (
        "Write a short, warm subscription-recovery message (max 40 words, no subject line, "
        "no placeholders). Indian consumer SaaS tone, plain and human, one clear next step.\n\n"
        f"Customer first name: {sub.customer.split()[0]}\n"
        f"Plan: {sub.plan}, amount: INR {sub.mrr:.0f}\n"
        f"Failure reason: {sub.failure_reason}\n"
        f"Chosen action: {playbook['action']} via {playbook['channel']}\n"
        f"Guidance: {playbook['why']}\n\n"
        "Return only the message text."
    )


# -- deterministic fallback for root cause -----------------------------------

def rules_root_cause(e: dict) -> RootCause:
    seg = e["segment"]
    dom = e["dominant_error"]
    domain = e["fault_domain"]
    drop = e["drop_pts"] * 100
    cur = e["current_rate"] * 100
    base = e["baseline_rate"] * 100

    if domain == "gateway" and e.get("gateway_localised"):
        other = e.get("same_route_other_gateways_sr")
        other_txt = f" while the same route on other acquirers holds at {other*100:.0f}%" \
            if other is not None else ""
        narrative = (
            f"{seg} fell from {base:.0f}% to {cur:.0f}% ({drop:.0f} pts). Failures are "
            f"dominated by {dom} with elevated latency ({e['avg_failed_latency_ms']}ms){other_txt}. "
            f"The fault is localised to the {e['gateway']} acquirer route, not the issuer or method."
        )
        conf = 0.85
    elif domain == "gateway":
        narrative = (
            f"{seg} dropped {drop:.0f} pts to {cur:.0f}%, driven by transient {dom} "
            f"(avg failed latency {e['avg_failed_latency_ms']}ms). No single healthy alternate "
            f"acquirer stands out, consistent with a broad transient gateway wobble."
        )
        conf = 0.7
    elif domain == "bank":
        narrative = (
            f"{seg} fell {drop:.0f} pts to {cur:.0f}%. Failures are {dom}, an issuer-side "
            f"decision by {e['issuer']}. Rerouting the acquirer will not change the outcome."
        )
        conf = 0.8
    else:
        narrative = (
            f"{seg} degraded {drop:.0f} pts to {cur:.0f}% with {dom} ({domain}-side). "
            f"Not auto-recoverable from the gateway."
        )
        conf = 0.6

    return RootCause(anomaly_id=seg, narrative=narrative, confidence=conf, source="rules")
