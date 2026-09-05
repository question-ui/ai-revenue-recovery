"""Synthetic payment stream. Realistic-enough to make detection and recovery meaningful,
deterministic-ish so a demo behaves. Supports injecting a degradation incident and
applying a reroute that lets a segment escape a gateway-scoped incident."""
from __future__ import annotations

import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from ..models import (
    Transaction, METHODS, ISSUERS, GATEWAYS, SUCCESS,
    GATEWAY_CODES, BANK_CODES, CUSTOMER_CODES,
)

# Baseline health. These are the "normal" success rates the world runs at.
BASE_SR = {"UPI": 0.965, "CARD": 0.93, "NETBANKING": 0.90, "WALLET": 0.955}

# Popularity weights so the mix looks like an Indian PG.
METHOD_W = {"UPI": 0.58, "CARD": 0.22, "NETBANKING": 0.10, "WALLET": 0.10}
ISSUER_W = {"HDFC": 0.28, "SBI": 0.24, "ICICI": 0.20, "AXIS": 0.16, "KOTAK": 0.12}
GATEWAY_W = {"GW_ALPHA": 0.45, "GW_BETA": 0.35, "GW_GAMMA": 0.20}

AMOUNT_BUCKETS = [(120, 0.35), (750, 0.30), (2500, 0.20), (9000, 0.12), (30000, 0.03)]

# When a txn fails "normally" (no incident), pick a plausible cause.
BASELINE_FAIL_MIX = {
    "INSUFFICIENT_FUNDS": 0.34, "BANK_DECLINE": 0.20, "AUTH_FAILED": 0.14,
    "GATEWAY_TIMEOUT": 0.10, "INVALID_VPA": 0.09, "DO_NOT_HONOR": 0.07,
    "GATEWAY_ERROR": 0.04, "RISK_BLOCKED": 0.02,
}


def _weighted(weights: dict[str, float]) -> str:
    keys = list(weights)
    return random.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _pick_amount() -> float:
    vals = [a for a, _ in AMOUNT_BUCKETS]
    w = [p for _, p in AMOUNT_BUCKETS]
    base = random.choices(vals, weights=w, k=1)[0]
    return round(base * random.uniform(0.6, 1.4), 2)


@dataclass
class Incident:
    """A degradation event scoped to a set of dimensions."""
    method: Optional[str] = None
    issuer: Optional[str] = None
    gateway: Optional[str] = None
    error_code: str = "GATEWAY_TIMEOUT"
    extra_fail_prob: float = 0.45   # added failure probability on matching txns
    label: str = ""

    def matches(self, method: str, issuer: str, gateway: str) -> bool:
        return (
            (self.method is None or self.method == method)
            and (self.issuer is None or self.issuer == issuer)
            and (self.gateway is None or self.gateway == gateway)
        )


@dataclass
class Reroute:
    """A recovery override: send a (method, issuer) segment to a healthy gateway."""
    method: str
    issuer: str
    target_gateway: str


class PaymentSimulator:
    def __init__(self, seed: int = 7):
        self._rng = random.Random(seed)
        random.seed(seed)
        self._counter = itertools.count(1)
        self.incident: Optional[Incident] = None
        self.reroute: Optional[Reroute] = None

    # -- incident / recovery controls --------------------------------------
    def trigger_incident(self, incident: Optional[Incident] = None) -> Incident:
        if incident is None:
            # A gateway-scoped UPI timeout storm on one bank+gateway — the classic
            # "one acquirer route goes bad" degradation, which is reroutable.
            incident = Incident(
                method="UPI", issuer="HDFC", gateway="GW_ALPHA",
                error_code="GATEWAY_TIMEOUT", extra_fail_prob=0.5,
                label="UPI / HDFC on GW_ALPHA timing out",
            )
        self.incident = incident
        self.reroute = None
        return incident

    def clear_incident(self) -> None:
        self.incident = None

    def apply_reroute(self, method: str, issuer: str, target_gateway: str) -> None:
        self.reroute = Reroute(method=method, issuer=issuer, target_gateway=target_gateway)

    # -- generation --------------------------------------------------------
    def _resolve_gateway(self, method: str, issuer: str) -> str:
        if self.reroute and self.reroute.method == method and self.reroute.issuer == issuer:
            return self.reroute.target_gateway
        return _weighted(GATEWAY_W)

    def next_batch(self, n: int) -> list[Transaction]:
        now = time.time()
        out: list[Transaction] = []
        for _ in range(n):
            method = _weighted(METHOD_W)
            issuer = _weighted(ISSUER_W)
            gateway = self._resolve_gateway(method, issuer)

            sr = BASE_SR[method] + self._rng.uniform(-0.012, 0.012)
            forced_code: Optional[str] = None

            if self.incident and self.incident.matches(method, issuer, gateway):
                sr = max(0.05, sr - self.incident.extra_fail_prob)
                # 80% of the incident's failures carry its signature error code.
                if self._rng.random() < 0.8:
                    forced_code = self.incident.error_code

            ok = self._rng.random() < sr
            if ok:
                status = SUCCESS
                latency = int(self._rng.gauss(320, 90))
            else:
                status = forced_code or _weighted(BASELINE_FAIL_MIX)
                latency = int(self._rng.gauss(1500, 500)) if status in GATEWAY_CODES \
                    else int(self._rng.gauss(600, 150))

            out.append(Transaction(
                txn_id=f"pay_{next(self._counter):08d}",
                ts=now,
                amount=_pick_amount(),
                method=method,
                issuer=issuer,
                gateway=gateway,
                status=status,
                latency_ms=max(40, latency),
            ))
        return out
