"""Regressions for defects found in the 2026-09 review (paper/code mismatches)."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from xair.core.context_store import RedisContextStore
from xair.core.context_validator import check_expression, evaluate_predicates
from xair.core.lifecycle import InvalidTransition
from xair.core.models import ActionIntent, DecisionOutcome, IntentState
from xair.core.runtime import XAIRRuntime, percentile


def _intent(**kw) -> ActionIntent:
    body = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).isoformat(),
        "freshness_window_ms": 1000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }
    body.update(kw)
    return ActionIntent.from_dict(body)


@pytest.mark.parametrize("expr,ctx", [
    ("robot.speed < 'fast'", {"robot": {"speed": 0.1}}),   # number vs non-numeric string
    ("line.state > 3", {"line": {"state": "RUN"}}),        # ordering on a string
    ("robot.moving == 1", {"robot": {"moving": True}}),    # bool vs number
    ("line.state == true", {"line": {"state": "RUN"}}),    # string vs bool
])
def test_ill_typed_predicates_fail_closed_without_raising(expr, ctx):
    ok, reason = check_expression(expr, ctx)
    assert not ok and reason.startswith("ill_typed")


def test_missing_path_fails_closed():
    ok, reason = check_expression("robot.speed < 0.1", {})
    assert not ok and reason.startswith("missing_path")


def test_equals_inside_literal_is_not_rewritten():
    assert check_expression("tag.value == 'a=b'", {"tag": {"value": "a=b"}})[0]


def test_numeric_string_from_mes_is_compared_numerically():
    assert check_expression("robot.speed <= 0.5", {"robot": {"speed": "0.25"}})[0]


def test_safety_constraints_evaluated_before_preconditions():
    ok, reason = evaluate_predicates(["human_proximity_m > 0.5"], ["line.state == 'RUN'"],
                                     {"human_proximity_m": 0.2, "line": {"state": "RUN"}})
    assert not ok and reason.startswith("safety_constraint_failed")


def test_publication_of_revoked_intent_is_refused():
    rt = XAIRRuntime(context={"line": {"state": "PAUSED"}})
    intent = _intent()
    rt.process_intent(intent)
    with pytest.raises(InvalidTransition):
        rt.confirm_publication(intent.id, True, "late")
    assert rt.lifecycle.get(intent.id).state == IntentState.REVOKED


def test_newer_version_at_publish_blocks_release():
    rt = XAIRRuntime()
    rec = rt.process_intent(_intent(), context={"line": {"state": "RUN"}}, context_version=3)
    final = rt.confirm_publication(rec.intent.id, True, "gate_passed", context_version=4)
    assert final.publication_decision == "BLOCK"
    assert final.reason == "context_version_changed_at_publish"


def test_supervisory_revoke_cannot_roll_back_executed_intent():
    rt = XAIRRuntime(context={"line": {"state": "RUN"}})
    intent = _intent()
    rt.process_intent(intent)
    rt.confirm_publication(intent.id, True, "ok")
    with pytest.raises(InvalidTransition):
        rt.supervisory_revoke(intent.id)


def test_idempotency_window_expires():
    rt = XAIRRuntime(context={"line": {"state": "RUN"}}, idempotency_retention_s=0.05)
    intent = _intent()
    _, dup1 = rt.admit(intent)
    _, dup2 = rt.admit(intent)
    assert (dup1, dup2) == (False, True)
    time.sleep(0.08)
    assert not rt.is_duplicate(intent.id)


def test_http_style_processing_does_not_leave_ghost_queue_entries():
    rt = XAIRRuntime(context={"line": {"state": "RUN"}})
    for _ in range(5):
        intent = _intent(payload={"action_type": "RESUME", "target_entity": str(uuid.uuid4())})
        rt.admit(intent)
        assert rt.process_intent(intent).outcome == DecisionOutcome.EXECUTE
    assert len(rt.receiver) == 0


def test_in_memory_store_version_is_monotonic_under_concurrency():
    store = RedisContextStore("")
    threads = [threading.Thread(target=lambda: [store.update({"k": i}) for i in range(200)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _, version, trusted = store.snapshot()
    assert trusted and version == 1600


def test_unreachable_redis_is_untrusted_and_does_not_advance_version():
    store = RedisContextStore("redis://127.0.0.1:1/0")
    assert store.update({"line": {"state": "RUN"}}) == 0
    _, version, trusted = store.snapshot()
    assert not trusted and version == 0


def test_nearest_rank_percentile():
    values = list(range(1, 101))
    assert percentile(values, 0.50) == 50
    assert percentile(values, 0.99) == 99
    assert percentile(values, 1.0) == 100
