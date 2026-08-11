from datetime import datetime, timezone

from xair.core.models import ActionDescriptor, ActionIntent, DecisionOutcome, IntentState
from xair.core.runtime import XAIRRuntime


def test_created_to_revoked_path():
    rt = XAIRRuntime()
    intent = ActionIntent(
        id="fsm-1",
        source="ai",
        timestamp_decision=datetime(2026, 1, 1, tzinfo=timezone.utc),
        freshness_window_ms=10,
        payload=ActionDescriptor("STOP", "r1"),
    )
    now = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    record = rt.process_intent(intent, now=now)
    states = [e["state"] for e in rt.lifecycle.audit_log if e["intent_id"] == "fsm-1"]
    assert "CREATED" in states
    assert "VALIDATING" in states
    assert record.state == IntentState.REVOKED
    assert record.outcome == DecisionOutcome.REVOKE
