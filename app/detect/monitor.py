"""Rolling success-rate monitor.

Detection runs at the *route* level (method / issuer), because that's the grain a
degradation actually lives at: a gateway fault hits one route via one acquirer, and
an issuer decline hits one route across every acquirer. Whether the fault is
localised to a single gateway is decided later, in root-cause. The health table is
shown at the finer method/issuer/gateway grain for operator detail.

  - current stats: computed over a recent slice of the window
  - baseline: a slow EWMA per route that only learns while the route looks healthy,
    so an ongoing incident never gets normalised away
  - detector: two-proportion z-test (current vs baseline) + absolute-drop floor +
    minimum sample size, so low-volume noise doesn't fire
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

from ..config import settings
from ..models import Transaction, SegmentHealth, fault_domain


class Monitor:
    def __init__(self, window_size: int | None = None):
        self.window: deque[Transaction] = deque(maxlen=window_size or settings.window_size)
        self._baseline: dict[str, float] = {}   # route "method/issuer" -> EWMA success rate
        self._ewma_alpha = 0.02

    def ingest(self, txns: list[Transaction]) -> None:
        for t in txns:
            self.window.append(t)

    def recent(self) -> list[Transaction]:
        return list(self.window)[-settings.recent_window:]

    # -- generic aggregation ----------------------------------------------
    @staticmethod
    def _blank():
        return {"total": 0, "success": 0, "at_risk": 0.0,
                "errors": defaultdict(int), "method": "", "issuer": "", "gateway": ""}

    def _aggregate(self, keyfn) -> dict:
        agg = defaultdict(self._blank)
        for t in self.recent():
            a = agg[keyfn(t)]
            a["method"], a["issuer"], a["gateway"] = t.method, t.issuer, t.gateway
            a["total"] += 1
            if t.ok:
                a["success"] += 1
            else:
                a["at_risk"] += t.amount
                a["errors"][t.status] += 1
        return agg

    # -- baselines (route grain) ------------------------------------------
    def _update_baselines(self, routes: dict) -> None:
        for key, a in routes.items():
            if a["total"] < 20:
                continue
            sr = a["success"] / a["total"]
            prev = self._baseline.get(key)
            if prev is None:
                self._baseline[key] = sr
            elif sr >= prev - 0.05:      # healthy-ish -> track
                self._baseline[key] = (1 - self._ewma_alpha) * prev + self._ewma_alpha * sr
            # degraded -> freeze baseline at its healthy value

    def baseline(self, key: str, fallback: float) -> float:
        return self._baseline.get(key, fallback)

    # -- display: fine-grained health -------------------------------------
    def segment_health(self, top: int = 12) -> list:
        agg = self._aggregate(lambda t: t.segment)
        out = []
        for seg, a in agg.items():
            if a["total"] < 8:
                continue
            sr = a["success"] / a["total"]
            route = f"{a['method']}/{a['issuer']}"
            dom = max(a["errors"], key=a["errors"].get) if a["errors"] else None
            out.append(SegmentHealth(
                key=seg, method=a["method"], issuer=a["issuer"], gateway=a["gateway"],
                total=a["total"], success=a["success"], success_rate=round(sr, 4),
                baseline_rate=round(self.baseline(route, sr), 4),
                dominant_error=dom, at_risk_amount=round(a["at_risk"], 2),
            ))
        out.sort(key=lambda s: (s.success_rate, -s.total))
        return out[:top]

    def overall(self) -> dict:
        recent = self.recent()
        total = len(recent)
        success = sum(1 for t in recent if t.ok)
        at_risk = sum(t.amount for t in recent if not t.ok)
        return {
            "total": total, "success": success,
            "success_rate": round(success / total, 4) if total else 1.0,
            "at_risk_amount": round(at_risk, 2),
        }

    # -- detection (route grain) ------------------------------------------
    def detect(self) -> dict | None:
        routes = self._aggregate(lambda t: f"{t.method}/{t.issuer}")
        self._update_baselines(routes)

        best = None
        for key, a in routes.items():
            n = a["total"]
            if n < settings.min_segment_samples:
                continue
            cur = a["success"] / n
            base = self.baseline(key, cur)
            drop = base - cur
            if drop < settings.min_absolute_drop:
                continue
            z = _two_proportion_z(base, cur, n)
            if z < settings.z_threshold:
                continue

            dom = max(a["errors"], key=a["errors"].get) if a["errors"] else "UNKNOWN"
            worst_gw = self._worst_gateway(a["method"], a["issuer"])
            cand = {
                "segment": key, "method": a["method"], "issuer": a["issuer"],
                "gateway": worst_gw,
                "baseline_rate": round(base, 4), "current_rate": round(cur, 4),
                "drop_pts": round(drop, 4), "z_score": round(z, 2),
                "dominant_error": dom, "fault_domain": fault_domain(dom),
                "sample_size": n,
            }
            if best is None or cand["drop_pts"] > best["drop_pts"]:
                best = cand
        return best

    def _worst_gateway(self, method: str, issuer: str) -> str:
        counts = defaultdict(lambda: [0, 0])  # gw -> [fails, total]
        for t in self.recent():
            if t.method == method and t.issuer == issuer:
                counts[t.gateway][1] += 1
                if not t.ok:
                    counts[t.gateway][0] += 1
        if not counts:
            return ""
        return max(counts, key=lambda gw: counts[gw][0])


def _two_proportion_z(p_base: float, p_cur: float, n: float) -> float:
    p = min(max(p_base, 1e-6), 1 - 1e-6)
    se = math.sqrt(p * (1 - p) / max(n, 1))
    return 0.0 if se == 0 else abs(p_base - p_cur) / se
