"""Core logic tests: the detect -> root-cause -> recover spine, plus subscriptions.
Run with: python -m pytest -q"""
from app.detect.monitor import Monitor
from app.detect.rootcause import build_evidence
from app.recover import policy
from app.recover.subscription_agent import build_plans, funnel
from app.simulator.payments import PaymentSimulator, Incident
from app.simulator.subscriptions import generate_failed_subscriptions


def _warm(sim, mon, ticks=25, n=60):
    for _ in range(ticks):
        mon.ingest(sim.next_batch(n))
        mon.detect()


def test_healthy_stream_no_false_positive():
    sim, mon = PaymentSimulator(), Monitor()
    _warm(sim, mon)
    assert mon.detect() is None, "should not fire on a healthy stream"


def test_gateway_incident_is_detected_and_localised():
    sim, mon = PaymentSimulator(), Monitor()
    _warm(sim, mon)
    sim.trigger_incident(Incident(method="UPI", issuer="HDFC", gateway="GW_ALPHA",
                                  error_code="GATEWAY_TIMEOUT", extra_fail_prob=0.5))
    for _ in range(20):
        mon.ingest(sim.next_batch(80))
    found = mon.detect()
    assert found is not None, "degradation must be detected"
    assert found["method"] == "UPI" and found["issuer"] == "HDFC"
    assert found["fault_domain"] == "gateway"

    ev = build_evidence(mon, found)
    act = policy.decide(found, ev, mon)
    # A localised gateway fault should be handled by rerouting to a healthy acquirer.
    assert act.kind in ("REROUTE", "RETRY_WITH_BACKOFF")
    if act.kind == "REROUTE":
        assert act.target_gateway and act.target_gateway != "GW_ALPHA"
        assert act.projected_recovered_txns >= 0


def test_bank_decline_is_not_rerouted():
    sim, mon = PaymentSimulator(), Monitor()
    _warm(sim, mon)
    sim.trigger_incident(Incident(method="CARD", issuer="SBI", gateway=None,
                                  error_code="BANK_DECLINE", extra_fail_prob=0.5))
    for _ in range(20):
        mon.ingest(sim.next_batch(80))
    found = mon.detect()
    assert found is not None
    ev = build_evidence(mon, found)
    act = policy.decide(found, ev, mon)
    assert act.kind in ("CIRCUIT_BREAK", "ALERT_ONLY")
    assert act.target_gateway is None


def test_reroute_recovers_success_rate():
    sim, mon = PaymentSimulator(), Monitor()
    _warm(sim, mon)
    sim.trigger_incident(Incident(method="UPI", issuer="HDFC", gateway="GW_ALPHA",
                                  error_code="GATEWAY_TIMEOUT", extra_fail_prob=0.5))
    for _ in range(15):
        mon.ingest(sim.next_batch(80))
    sim.apply_reroute("UPI", "HDFC", "GW_BETA")
    fresh = Monitor()
    for _ in range(15):
        fresh.ingest(sim.next_batch(80))
    seg = [t for t in fresh.window if t.method == "UPI" and t.issuer == "HDFC"]
    sr = sum(1 for t in seg if t.ok) / len(seg)
    assert sr > 0.9, f"reroute should restore health, got {sr:.2f}"


def test_subscription_recovery_playbook():
    subs = generate_failed_subscriptions(n=24)
    plans = build_plans(subs, use_llm=False)
    assert len(plans) == 24
    by_reason = {p.failure_reason: p for p in plans}
    # Expired cards must not be blindly retried.
    if "CARD_EXPIRED" in by_reason:
        assert by_reason["CARD_EXPIRED"].action == "UPDATE_INSTRUMENT"
    f = funnel(subs, plans)
    assert 0 <= f["recovery_rate"] <= 1
    assert f["expected_recovered_mrr"] <= f["at_risk_mrr"]
