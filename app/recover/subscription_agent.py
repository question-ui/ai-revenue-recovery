"""Failed-subscription recovery.

For each failed renewal, choose an action tuned to *why* it failed, pick the right
customer channel, generate a dunning message (LLM if configured, else a template),
and estimate recovery probability so we can project recovered MRR.
"""
from __future__ import annotations

from ..config import settings
from ..llm.client import LLMClient
from ..models import Subscription, SubRecoveryPlan


# Stopping rules: after this many failed attempts, auto-recovery stops and the
# account escalates to a human instead of being contacted again automatically.
# This bounds both wasted retries and repeated customer contact.
MAX_AUTO_ATTEMPTS = 3
MIN_HOURS_BETWEEN_CONTACTS = 24  # documented floor; the batch model checks days_since_fail

# Per-reason playbook: action, retry timing, channel, and a base recovery probability
# that reflects how salvageable that failure class typically is.
PLAYBOOK = {
    "INSUFFICIENT_FUNDS": {
        "action": "SMART_RETRY", "retry_in_days": 3, "channel": "SMS", "base_prob": 0.62,
        "why": "Balance shortfalls usually clear within a few days; retry near a likely credit date.",
    },
    "CARD_EXPIRED": {
        "action": "UPDATE_INSTRUMENT", "retry_in_days": None, "channel": "EMAIL", "base_prob": 0.48,
        "why": "The card is dead; retrying is pointless. Drive a card update, then charge.",
    },
    "DO_NOT_HONOR": {
        "action": "RETRY_THEN_ESCALATE", "retry_in_days": 2, "channel": "WHATSAPP", "base_prob": 0.4,
        "why": "Ambiguous issuer decline; retry once, then ask the customer to authorise.",
    },
    "TECHNICAL_DECLINE": {
        "action": "RETRY_NOW", "retry_in_days": 0, "channel": "NONE", "base_prob": 0.75,
        "why": "Transient technical failure; an immediate silent retry usually succeeds.",
    },
}


def _template_message(sub: Subscription) -> str:
    p = PLAYBOOK[sub.failure_reason]
    name = sub.customer.split()[0]
    if p["action"] == "UPDATE_INSTRUMENT":
        return (f"Hi {name}, we couldn't renew your {sub.plan} plan because your card has "
                f"expired. Update your card in a tap to keep your access active \u2014 it "
                f"takes under a minute.")
    if p["action"] == "SMART_RETRY":
        return (f"Hi {name}, your {sub.plan} renewal (\u20b9{sub.mrr:.0f}) didn't go through. "
                f"We'll try again in {p['retry_in_days']} days \u2014 no action needed if funds "
                f"are available by then.")
    if p["action"] == "RETRY_THEN_ESCALATE":
        return (f"Hi {name}, your bank declined the {sub.plan} renewal. We'll retry once; if "
                f"it fails again, please approve the payment with your bank or update your method.")
    return ""  # RETRY_NOW is silent


def _escalation_message(sub: Subscription) -> str:
    name = sub.customer.split()[0]
    return (f"Hi {name}, we've tried a few times to renew your {sub.plan} plan without success. "
            f"To avoid further automated attempts, a member of our team will reach out directly "
            f"to help sort this out.")


def _probability(sub: Subscription) -> float:
    base = PLAYBOOK[sub.failure_reason]["base_prob"]
    # More prior attempts -> lower odds. Recency helps a little.
    base *= max(0.4, 1.0 - 0.12 * (sub.attempts - 1))
    base *= 1.0 - 0.03 * sub.days_since_fail
    return round(min(0.9, max(0.05, base)), 3)


def build_plans(subs: list[Subscription], use_llm: bool = True) -> list[SubRecoveryPlan]:
    llm = LLMClient() if (use_llm and settings.llm_enabled) else None
    plans: list[SubRecoveryPlan] = []
    for sub in subs:
        # --- Stopping rule -------------------------------------------------
        # Past MAX_AUTO_ATTEMPTS, stop auto-retrying and stop auto-contacting the
        # customer. Hand off to a human instead of retrying or messaging forever.
        if sub.attempts >= MAX_AUTO_ATTEMPTS:
            message = _escalation_message(sub)
            plans.append(SubRecoveryPlan(
                sub_id=sub.sub_id, customer=sub.customer, plan=sub.plan, mrr=sub.mrr,
                failure_reason=sub.failure_reason, action="ESCALATE_TO_HUMAN",
                retry_in_days=None, channel="EMAIL",
                message=message, recovery_probability=0.15,
                expected_recovered_mrr=round(sub.mrr * 0.15, 2), message_source="template",
            ))
            continue

        p = PLAYBOOK[sub.failure_reason]
        prob = _probability(sub)

        if p["channel"] == "NONE":
            message, source = "", "template"
        elif llm is not None:
            message, source = llm.dunning_message(sub, p), f"llm:{settings.llm_provider}"
            if not message:
                message, source = _template_message(sub), "template"
        else:
            message, source = _template_message(sub), "template"

        plans.append(SubRecoveryPlan(
            sub_id=sub.sub_id, customer=sub.customer, plan=sub.plan, mrr=sub.mrr,
            failure_reason=sub.failure_reason, action=p["action"],
            retry_in_days=p["retry_in_days"], channel=p["channel"],
            message=message, recovery_probability=prob,
            expected_recovered_mrr=round(sub.mrr * prob, 2), message_source=source,
        ))
    return plans


def funnel(subs: list[Subscription], plans: list[SubRecoveryPlan]) -> dict:
    at_risk = sum(s.mrr for s in subs)
    expected = sum(p.expected_recovered_mrr for p in plans)
    by_reason: dict[str, dict] = {}
    for s, p in zip(subs, plans):
        b = by_reason.setdefault(s.failure_reason, {"count": 0, "at_risk": 0.0, "expected": 0.0})
        b["count"] += 1
        b["at_risk"] += s.mrr
        b["expected"] += p.expected_recovered_mrr
    for b in b_reason.values() if False else by_reason.values():
        b["at_risk"] = round(b["at_risk"], 2)
        b["expected"] = round(b["expected"], 2)
    return {
        "at_risk_mrr": round(at_risk, 2),
        "expected_recovered_mrr": round(expected, 2),
        "recovery_rate": round(expected / at_risk, 4) if at_risk else 0.0,
        "count": len(subs),
        "by_reason": by_reason,
    }
