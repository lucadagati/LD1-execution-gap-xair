from datetime import datetime, timezone

from xair.core.models import ActionDescriptor, ActionIntent
from xair.core.runtime import XAIRRuntime


def test_idempotent_submit():
    rt = XAIRRuntime(context={"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    intent = ActionIntent(
        id="idempotent-1",
        source="ai",
        timestamp_decision=datetime.now(timezone.utc),
        freshness_window_ms=800,
        preconditions=["line.state == 'RUN'"],
        payload=ActionDescriptor("RESUME", "robot_3"),
    )
    r1 = rt.submit_intent(intent)
    r2 = rt.submit_intent(intent)
    assert r1.intent.id == r2.intent.id
    assert rt._metrics["intents_received"] == 1
