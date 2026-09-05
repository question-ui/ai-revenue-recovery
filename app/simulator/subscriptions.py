"""Synthetic book of subscriptions whose latest renewal failed."""
from __future__ import annotations

import random

from ..models import Subscription, METHODS, ISSUERS, PLANS, SUB_FAILURE_REASONS

FIRST = ["Aarav", "Diya", "Vivaan", "Ananya", "Kabir", "Isha", "Reyansh", "Myra",
         "Arjun", "Saanvi", "Vihaan", "Aadhya", "Rohan", "Kiara", "Aditya", "Zara"]
LAST = ["Sharma", "Patel", "Reddy", "Nair", "Iyer", "Gupta", "Mehta", "Singh",
        "Rao", "Bose", "Kapoor", "Menon", "Das", "Joshi"]

REASON_W = {"INSUFFICIENT_FUNDS": 0.42, "CARD_EXPIRED": 0.24,
            "DO_NOT_HONOR": 0.18, "TECHNICAL_DECLINE": 0.16}


def _weighted(weights: dict[str, float]) -> str:
    keys = list(weights)
    return random.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def generate_failed_subscriptions(n: int = 24, seed: int = 11) -> list[Subscription]:
    rng = random.Random(seed)
    random.seed(seed)
    subs: list[Subscription] = []
    for i in range(n):
        plan, mrr = rng.choice(PLANS)
        reason = _weighted(REASON_W)
        subs.append(Subscription(
            sub_id=f"sub_{1000+i}",
            customer=f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            plan=plan,
            mrr=mrr,
            method="CARD" if reason == "CARD_EXPIRED" else rng.choice(METHODS),
            issuer=rng.choice(ISSUERS),
            failure_reason=reason,
            attempts=rng.randint(1, 3),
            days_since_fail=rng.randint(0, 6),
        ))
    return subs
