from xair.core.context_validator import ContextValidator


def test_deep_merge_context():
    v = ContextValidator({"line": {"state": "RUN", "speed": 1}, "gripper": {"state": "OPEN"}})
    v.update_context({"line": {"state": "PAUSED"}})
    assert v.context["line"]["state"] == "PAUSED"
    assert v.context["line"]["speed"] == 1
    ok, _ = v.validate(__import__("xair.core.models", fromlist=["ActionIntent"]).ActionIntent.from_dict({
        "id": "t1", "source": "ai", "timestamp_decision": "2025-01-01T00:00:00Z",
        "freshness_window_ms": 500, "preconditions": ["line.state == 'RUN'"],
        "payload": {"action_type": "RESUME", "target_entity": "x", "parameters": {}},
    }))
    assert not ok
