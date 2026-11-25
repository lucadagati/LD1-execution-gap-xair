from datetime import datetime, timedelta, timezone

from xair.core.models import ActionDescriptor, ActionIntent, DecisionOutcome
from xair.core.runtime import XAIRRuntime


def test_context_revoke_on_line_paused():
    rt = XAIRRuntime(context={"line": {"state": "PAUSED"}, "robot": {"speed": 0.0}})
    intent = ActionIntent(
        id="ctx-1",
        source="ai",
        timestamp_decision=datetime.now(timezone.utc),
        freshness_window_ms=800,
        preconditions=["line.state != 'PAUSED'"],
        payload=ActionDescriptor("STOP_ROBOT", "robot_3"),
    )
    record = rt.process_intent(intent)
    assert record.outcome == DecisionOutcome.REVOKE
    assert "precondition" in record.reason


def test_safety_constraint_human_proximity():
    rt = XAIRRuntime(context={"human_proximity_m": 0.3, "robot": {"speed": 0.0}})
    intent = ActionIntent(
        id="ctx-2",
        source="ai",
        timestamp_decision=datetime.now(timezone.utc),
        freshness_window_ms=800,
        safety_constraints=["human_proximity_m > 0.5"],
        payload=ActionDescriptor("MOVE", "robot_3"),
    )
    record = rt.process_intent(intent)
    assert record.outcome == DecisionOutcome.REVOKE
