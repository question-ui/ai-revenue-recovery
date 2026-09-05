"""The spine: detect -> root cause -> recover, over a live synthetic stream.

Holds all live state for the dashboard. One Engine instance per process.
"""
from __future__ import annotations

import time
from dataclasses import asdict

from .config import settings
from .detect.monitor import Monitor
from .detect.rootcause import build_evidence
from .llm.client import LLMClient
from .models import Anomaly, RootCause, RecoveryAction
from .recover import policy
from .recover.subscription_agent import build_plans, funnel
from .simulator.payments import PaymentSimulator
from .simulator.subscriptions import generate_failed_subscriptions


class Engine:
    def __init__(self):
        self.sim = PaymentSimulator()
        self.monitor = Monitor()
        self.ticks = 0
        self.recovered_amount = 0.0     # realised recovered revenue after actions applied
        self.recovered_txns = 0

        # active incident state
        self.anomaly: dict | None = None
        self.root_cause: RootCause | None = None
        self.action: RecoveryAction | None = None
        self.action_applied_tick: int | None = None
        self.events: list[dict] = []    # activity log (newest first)

        # subscriptions
        self.subscriptions = generate_failed_subscriptions()
        self.sub_plans = []
        self.sub_funnel = {}

    # -- lifecycle ---------------------------------------------------------
    def warmup(self, ticks: int = 20) -> None:
        """Fill the window with healthy traffic so baselines settle before any incident."""
        for _ in range(ticks):
            self.monitor.ingest(self.sim.next_batch(settings.txns_per_tick))
            self.monitor.detect()  # advances baselines

    def tick(self) -> None:
        self.ticks += 1
        self.monitor.ingest(self.sim.next_batch(settings.txns_per_tick))

        # Track realised recovery: once an action is applied, count the extra successes
        # on the recovered route versus the degraded rate.
        if self.action and self.action.applied and self.anomaly:
            self._accrue_recovery()

        if self.anomaly is None:
            found = self.monitor.detect()
            if found:
                self._on_anomaly(found)
        else:
            # Has the segment healed (e.g. after a reroute)? Then resolve.
            self._maybe_resolve()

    # -- incident handling -------------------------------------------------
    def _on_anomaly(self, found: dict) -> None:
        self.anomaly = found
        evidence = build_evidence(self.monitor, found)
        self.root_cause = LLMClient().root_cause(evidence)
        self.action = policy.decide(found, evidence, self.monitor)
        self._log("anomaly",
                  f"Degradation on {found['segment']}: {found['current_rate']*100:.0f}% "
                  f"(baseline {found['baseline_rate']*100:.0f}%), {found['dominant_error']}")
        self._log("rca", self.root_cause.narrative)
        self._log("action", f"Recommended: {self.action.title}")

    def apply_action(self) -> dict:
        if not self.action:
            return {"ok": False, "error": "No recovery action to apply."}
        if self.action.applied:
            return {"ok": False, "error": "Action already applied."}
        self.action.applied = True
        self.action_applied_tick = self.ticks
        if self.action.kind == "REROUTE" and self.action.target_gateway:
            self.sim.apply_reroute(self.anomaly["method"], self.anomaly["issuer"],
                                   self.action.target_gateway)
            self._log("applied", f"Rerouted {self.anomaly['method']}/{self.anomaly['issuer']} "
                                 f"to {self.action.target_gateway}")
        elif self.action.kind == "RETRY_WITH_BACKOFF":
            # Model retries as recovering a fraction of the incident directly.
            self.sim.incident.extra_fail_prob *= 0.4 if self.sim.incident else 1
            self._log("applied", "Enabled backoff retries with idempotency keys")
        elif self.action.kind == "CIRCUIT_BREAK":
            self.sim.clear_incident()
            self._log("applied", f"Circuit-broke {self.anomaly['issuer']}; surfaced alt method")
        else:
            self._log("applied", self.action.title)
        return {"ok": True}

    def _accrue_recovery(self) -> None:
        """Estimate recovered revenue this tick vs the counterfactual do-nothing rate."""
        a = self.anomaly
        method, issuer = a["method"], a["issuer"]
        recent = [t for t in list(self.monitor.window)[-settings.txns_per_tick:]
                  if t.method == method and t.issuer == issuer]
        if not recent:
            return
        healthy = sum(1 for t in recent if t.ok)
        degraded_rate = a["current_rate"]
        extra = healthy - int(round(len(recent) * degraded_rate))
        if extra > 0:
            avg_amt = sum(t.amount for t in recent) / len(recent)
            self.recovered_txns += extra
            self.recovered_amount += extra * avg_amt

    def _maybe_resolve(self) -> None:
        a = self.anomaly
        recent = [t for t in list(self.monitor.window)[-settings.txns_per_tick * 4:]
                  if t.method == a["method"] and t.issuer == a["issuer"]]
        if len(recent) < 30:
            return
        sr = sum(1 for t in recent if t.ok) / len(recent)
        if sr >= a["baseline_rate"] - 0.03:
            self._log("resolved", f"{a['method']}/{a['issuer']} recovered to {sr*100:.0f}%")
            self.anomaly = None  # keep root_cause/action for the panel until next incident

    def trigger_incident(self, kind: str = "gateway") -> dict:
        if kind == "bank":
            from .simulator.payments import Incident
            incident = Incident(
                method="CARD", issuer="SBI", gateway=None,
                error_code="BANK_DECLINE", extra_fail_prob=0.5,
                label="CARD / SBI declines spiking (issuer-side)",
            )
        else:
            incident = None  # simulator default: UPI/HDFC/GW_ALPHA timeout
        inc = self.sim.trigger_incident(incident)
        self.anomaly = None
        self.root_cause = None
        self.action = None
        self.action_applied_tick = None
        self.recovered_amount = 0.0
        self.recovered_txns = 0
        self._log("incident", f"Injected incident: {inc.label}")
        return {"ok": True, "incident": inc.label}

    # -- subscriptions -----------------------------------------------------
    def run_subscription_recovery(self) -> dict:
        self.sub_plans = build_plans(self.subscriptions, use_llm=True)
        self.sub_funnel = funnel(self.subscriptions, self.sub_plans)
        self._log("subs", f"Planned recovery for {len(self.sub_plans)} failed subscriptions; "
                          f"projected {settings.currency_symbol}"
                          f"{self.sub_funnel['expected_recovered_mrr']:.0f} MRR recoverable")
        return self.snapshot_subscriptions()

    # -- state for the UI --------------------------------------------------
    def _log(self, kind: str, text: str) -> None:
        self.events.insert(0, {"kind": kind, "text": text, "ts": time.time()})
        self.events = self.events[:40]

    def snapshot(self) -> dict:
        overall = self.monitor.overall()
        health = [s.to_dict() for s in self.monitor.segment_health()]
        return {
            "tick": self.ticks,
            "llm": {"enabled": settings.llm_enabled, "provider": settings.llm_provider},
            "currency_symbol": settings.currency_symbol,
            "overall": overall,
            "at_risk_amount": overall["at_risk_amount"],
            "recovered_amount": round(self.recovered_amount, 2),
            "recovered_txns": self.recovered_txns,
            "segments": health,
            "anomaly": self.anomaly,
            "incident_active": self.sim.incident is not None,
            "root_cause": asdict(self.root_cause) if self.root_cause else None,
            "action": asdict(self.action) if self.action else None,
            "events": self.events[:12],
        }

    def snapshot_subscriptions(self) -> dict:
        return {
            "currency_symbol": settings.currency_symbol,
            "funnel": self.sub_funnel,
            "plans": [asdict(p) for p in self.sub_plans],
            "count": len(self.subscriptions),
            "planned": bool(self.sub_plans),
        }
