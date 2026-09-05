"""Maps an anomaly + evidence to a concrete recovery action, and projects its uplift.

Decision logic (deterministic; the LLM only writes the human narrative, it never
overrides the action the policy is safe to take):

  gateway fault, localised to one acquirer  -> REROUTE to the healthiest alternate
  gateway fault, not clearly localised       -> RETRY_WITH_BACKOFF (idempotent retries)
  bank fault (issuer declines)               -> CIRCUIT_BREAK + ALERT (rerouting won't help)
  customer / risk fault                      -> ALERT_ONLY (nothing to auto-recover)
"""
from __future__ import annotations

from ..detect.monitor import Monitor
from ..models import RecoveryAction, GATEWAYS, GATEWAY_CODES


def _healthiest_alternate(monitor: Monitor, method: str, issuer: str, exclude: str) -> tuple[str, float]:
    """Best alternate gateway for this method/issuer by observed success rate."""
    best_gw, best_sr = None, -1.0
    for gw in GATEWAYS:
        if gw == exclude:
            continue
        txns = [t for t in monitor.recent()
                if t.method == method and t.issuer == issuer and t.gateway == gw]
        sr = (sum(1 for t in txns if t.ok) / len(txns)) if txns else 0.9
        if sr > best_sr:
            best_gw, best_sr = gw, sr
    return best_gw or GATEWAYS[0], max(best_sr, 0.85)


def decide(anomaly: dict, evidence: dict, monitor: Monitor) -> RecoveryAction:
    method, issuer, gateway = anomaly["method"], anomaly["issuer"], anomaly["gateway"]
    domain = anomaly["fault_domain"]
    cur = anomaly["current_rate"]

    # Volume of the failing segment over the window -> basis for projected recovery.
    seg_total = anomaly["sample_size"]
    seg_txns = [t for t in monitor.recent()
                if t.method == method and t.issuer == issuer and t.gateway == gateway]
    avg_amount = (sum(t.amount for t in seg_txns) / len(seg_txns)) if seg_txns else 800.0
    failing_now = sum(1 for t in seg_txns if not t.ok)

    if domain == "gateway" and evidence.get("gateway_localised"):
        target, target_sr = _healthiest_alternate(monitor, method, issuer, gateway)
        # Recoverable = the failing traffic stuck on the bad acquirer that a healthy
        # acquirer would have converted.
        impl = [t for t in monitor.recent()
                if t.method == method and t.issuer == issuer and t.gateway == gateway]
        impl_sr = evidence.get("implicated_gateway_sr") or cur
        recoverable_rate = max(0.0, target_sr - impl_sr)
        rec_txns = int(round(len(impl) * recoverable_rate))
        return RecoveryAction(
            anomaly_id=anomaly["segment"],
            kind="REROUTE",
            title=f"Reroute {method}/{issuer} to {target}",
            detail=(f"Fault is localised to {gateway} ({anomaly['dominant_error']}). "
                    f"{target} is healthy for this route at {target_sr:.0%}. "
                    f"Reroute new {method}/{issuer} traffic to {target}."),
            target_gateway=target,
            projected_recovered_txns=rec_txns,
            projected_recovered_amount=round(rec_txns * avg_amount, 2),
        )

    if domain == "gateway":
        recoverable_rate = min(0.6, anomaly["drop_pts"])  # retries claw back timeouts
        rec_txns = int(round(failing_now * recoverable_rate))
        return RecoveryAction(
            anomaly_id=anomaly["segment"],
            kind="RETRY_WITH_BACKOFF",
            title=f"Retry {anomaly['dominant_error']} on {method}/{issuer}",
            detail=("Transient gateway faults with no single healthy alternate. "
                    "Retry failed attempts with exponential backoff and idempotency keys."),
            target_gateway=None,
            projected_recovered_txns=rec_txns,
            projected_recovered_amount=round(rec_txns * avg_amount, 2),
        )

    if domain == "bank":
        return RecoveryAction(
            anomaly_id=anomaly["segment"],
            kind="CIRCUIT_BREAK",
            title=f"Circuit-break {issuer} + alert",
            detail=(f"Issuer-side declines ({anomaly['dominant_error']}). Rerouting the "
                    f"acquirer will not help. Throttle {method}/{issuer}, surface an alt "
                    f"method to customers, and alert the {issuer} relationship owner."),
            target_gateway=None,
            projected_recovered_txns=0,
            projected_recovered_amount=0.0,
        )

    return RecoveryAction(
        anomaly_id=anomaly["segment"],
        kind="ALERT_ONLY",
        title="Alert on-call",
        detail=(f"{anomaly['dominant_error']} is {domain}-side and not auto-recoverable. "
                f"Page on-call and monitor; no safe automated action."),
        target_gateway=None,
        projected_recovered_txns=0,
        projected_recovered_amount=0.0,
    )
