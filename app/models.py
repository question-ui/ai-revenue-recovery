"""Domain models shared across simulation, detection and recovery."""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional


# --- Payment taxonomy -------------------------------------------------------

METHODS = ["UPI", "CARD", "NETBANKING", "WALLET"]
ISSUERS = ["HDFC", "SBI", "ICICI", "AXIS", "KOTAK"]
GATEWAYS = ["GW_ALPHA", "GW_BETA", "GW_GAMMA"]  # acquirers / payment aggregators

SUCCESS = "SUCCESS"

# Failure codes grouped by where the fault lives. This drives the recovery decision.
GATEWAY_CODES = {"GATEWAY_TIMEOUT", "GATEWAY_ERROR", "NETWORK_ERROR"}   # reroutable + retryable
BANK_CODES = {"BANK_DECLINE", "INSUFFICIENT_FUNDS", "DO_NOT_HONOR"}      # issuer side
CUSTOMER_CODES = {"INVALID_VPA", "AUTH_FAILED", "EXPIRED_INSTRUMENT"}    # customer side
RISK_CODES = {"RISK_BLOCKED"}

RETRYABLE = GATEWAY_CODES | {"DO_NOT_HONOR"}
REROUTABLE = GATEWAY_CODES  # switching acquirer only helps for gateway-side faults


def fault_domain(code: str) -> str:
    if code in GATEWAY_CODES:
        return "gateway"
    if code in BANK_CODES:
        return "bank"
    if code in CUSTOMER_CODES:
        return "customer"
    if code in RISK_CODES:
        return "risk"
    return "unknown"


@dataclass
class Transaction:
    txn_id: str
    ts: float
    amount: float
    method: str
    issuer: str
    gateway: str
    status: str            # SUCCESS or a failure code
    latency_ms: int

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS

    @property
    def segment(self) -> str:
        return f"{self.method}/{self.issuer}/{self.gateway}"


@dataclass
class SegmentHealth:
    key: str
    method: str
    issuer: str
    gateway: str
    total: int
    success: int
    success_rate: float
    baseline_rate: float
    dominant_error: Optional[str]
    at_risk_amount: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Anomaly:
    id: str
    detected_ts: float
    segment: str
    method: str
    issuer: str
    gateway: str
    baseline_rate: float
    current_rate: float
    drop_pts: float
    z_score: float
    dominant_error: str
    fault_domain: str
    sample_size: int
    resolved: bool = False


@dataclass
class RootCause:
    anomaly_id: str
    narrative: str
    confidence: float
    source: str  # "llm:<provider>" or "rules"


@dataclass
class RecoveryAction:
    anomaly_id: str
    kind: str        # REROUTE | RETRY_WITH_BACKOFF | CIRCUIT_BREAK | ALERT_ONLY
    title: str
    detail: str
    target_gateway: Optional[str]
    projected_recovered_txns: int
    projected_recovered_amount: float
    applied: bool = False


# --- Subscription taxonomy --------------------------------------------------

SUB_FAILURE_REASONS = ["INSUFFICIENT_FUNDS", "CARD_EXPIRED", "DO_NOT_HONOR", "TECHNICAL_DECLINE"]

PLANS = [
    ("Starter", 499.0),
    ("Growth", 1499.0),
    ("Scale", 4999.0),
    ("Enterprise", 14999.0),
]


@dataclass
class Subscription:
    sub_id: str
    customer: str
    plan: str
    mrr: float
    method: str
    issuer: str
    failure_reason: str
    attempts: int
    days_since_fail: int


@dataclass
class SubRecoveryPlan:
    sub_id: str
    customer: str
    plan: str
    mrr: float
    failure_reason: str
    action: str            # SMART_RETRY | UPDATE_INSTRUMENT | RETRY_THEN_ESCALATE | RETRY_NOW
    retry_in_days: Optional[int]
    channel: str           # EMAIL | SMS | WHATSAPP | NONE
    message: str
    recovery_probability: float
    expected_recovered_mrr: float
    message_source: str    # "llm:<provider>" or "template"
