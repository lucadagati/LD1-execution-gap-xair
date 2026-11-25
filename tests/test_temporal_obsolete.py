from datetime import datetime, timedelta, timezone

from xair.core.models import ActionDescriptor, ActionIntent, DecisionOutcome, IntentState
from xair.core.runtime import XAIRRuntime


def _intent(freshness_ms=500, age_ms=0, **kwargs) -> ActionIntent:
    now = datetime.now(timezone.utc)
    ts = now - timedelta(milliseconds=age_ms)
    payload = ActionDescriptor(
        action_type=kwargs.get("action_type", "STOP_ROBOT"),
        target_entity=kwargs.get("target", "robot_3"),
    )
    return ActionIntent(
        id=kwargs.get("id", "test-intent-1"),
        source=kwargs.get("source", "ai"),
        timestamp_decision=ts,
        freshness_window_ms=freshness_ms,
        payload=payload,
        preconditions=kwargs.get("preconditions", []),
    )


def test_temporal_obsolete_revoked():
    rt = XAIRRuntime()
    intent = _intent(freshness_ms=200, age_ms=400)
    rt.submit_intent(intent)
    record = rt.process_intent(intent)
    assert record.outcome == DecisionOutcome.REVOKE
    assert record.state == IntentState.REVOKED


def test_temporal_fresh_executes():
    rt = XAIRRuntime(context={"robot": {"speed": 0.0}, "line": {"state": "RUN"}})
    intent = _intent(freshness_ms=500, age_ms=50, preconditions=["robot.speed < 0.1"])
    rt.submit_intent(intent)
    record = rt.process_intent(intent)
    assert record.outcome == DecisionOutcome.EXECUTE


def test_context_precondition_revoked():
    rt = XAIRRuntime(context={"robot": {"speed": 0.5}})
    intent = _intent(freshness_ms=500, age_ms=10, preconditions=["robot.speed < 0.1"])
    record = rt.process_intent(intent)
    assert record.outcome == DecisionOutcome.REVOKE


def test_fsm_audit_trail():
    rt = XAIRRuntime()
    intent = _intent(freshness_ms=100, age_ms=300)
    rt.process_intent(intent)
    assert len(rt.lifecycle.audit_log) >= 2
    assert rt.lifecycle.audit_log[-1]["state"] in ("REVOKED", "EXPIRED")
