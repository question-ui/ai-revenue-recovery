"""Structured root-cause localisation.

The detector fires at the route level (method/issuer). Here we decide whether the
route's failures concentrate on a single gateway (a localised acquirer fault, which
a reroute can fix) or are spread across all gateways (an issuer-side problem, which
rerouting won't touch). This is the deterministic evidence the LLM (or the
rule-based fallback) turns into a human explanation.
"""
from __future__ import annotations

from collections import defaultdict

from ..detect.monitor import Monitor


def _sr(txns) -> float | None:
    return (sum(1 for t in txns if t.ok) / len(txns)) if txns else None


def build_evidence(monitor: Monitor, anomaly: dict) -> dict:
    method, issuer, gateway = anomaly["method"], anomaly["issuer"], anomaly["gateway"]
    win = monitor.recent()

    route_txns = [t for t in win if t.method == method and t.issuer == issuer]
    impl = [t for t in route_txns if t.gateway == gateway]          # implicated gateway
    other = [t for t in route_txns if t.gateway != gateway]         # rest of the route

    impl_sr = _sr(impl)
    other_sr = _sr(other)

    fails = [t for t in route_txns if not t.ok]
    err_counts: dict[str, int] = defaultdict(int)
    for t in fails:
        err_counts[t.status] += 1
    err_dist = sorted(err_counts.items(), key=lambda kv: -kv[1])

    latency = [t.latency_ms for t in fails]
    avg_fail_latency = round(sum(latency) / len(latency)) if latency else 0

    localised = bool(
        impl_sr is not None and other_sr is not None
        and (other_sr - impl_sr) > 0.15
    )

    return {
        "segment": anomaly["segment"],
        "method": method, "issuer": issuer, "gateway": gateway,
        "baseline_rate": anomaly["baseline_rate"],
        "current_rate": anomaly["current_rate"],
        "drop_pts": anomaly["drop_pts"],
        "z_score": anomaly["z_score"],
        "sample_size": anomaly["sample_size"],
        "dominant_error": anomaly["dominant_error"],
        "fault_domain": anomaly["fault_domain"],
        "error_distribution": err_dist[:4],
        "implicated_gateway_sr": None if impl_sr is None else round(impl_sr, 4),
        "same_route_other_gateways_sr": None if other_sr is None else round(other_sr, 4),
        "avg_failed_latency_ms": avg_fail_latency,
        "gateway_localised": localised,
    }
