"""Freshness vs deadline, clock uncertainty, server-side policy, and the actuation-log consumer."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from xair.adapters.actuation_consumer import ActuationConsumer, ConsumerCrash, IdempotentActuator
from xair.core.context_store import RedisContextStore
from xair.core.models import ActionIntent, IntentState
from xair.core.runtime import XAIRRuntime
from xair.core.temporal_validator import TemporalValidator
from xair.core.versioning import read_set_version


def _intent(age_ms: float, w: int, d: int | None = None, pre=("line.state == 'RUN'",), action="RESUME") -> ActionIntent:
    return ActionIntent.from_dict({
        "id": str(uuid.uuid4()), "source": "ai",
        "timestamp_decision": (datetime.now(timezone.utc) - timedelta(milliseconds=age_ms)).isoformat(),
        "freshness_window_ms": w, **({"deadline_ms": d} if d is not None else {}),
        "preconditions": [{"expr": e} for e in pre],
        "payload": {"action_type": action, "target_entity": "line_1"},
    })


def test_freshness_bounds_validation_deadline_bounds_release():
    tv = TemporalValidator()
    late = _intent(300, w=200, d=1000)
    assert not tv.validate(late)[0]              # too old to be validated
    assert tv.validate_release(late)[0]          # still within its release deadline
    assert not tv.validate_release(_intent(300, w=1000, d=200))[0]
    assert not tv.validate_release(_intent(300, w=200))[0]  # no deadline: w bounds the release


def test_clock_uncertainty_is_added_to_the_age():
    assert TemporalValidator(clock_uncertainty_ms=0).validate(_intent(150, w=200))[0]
    assert not TemporalValidator(clock_uncertainty_ms=100).validate(_intent(150, w=200))[0]


def test_policy_adds_mandatory_predicates_the_producer_omitted():
    rt = XAIRRuntime(context={"line": {"state": "PAUSED"}})
    lax = _intent(0, w=1000, pre=())
    assert rt.process_intent(lax).state == IntentState.AUTHORIZED
    rt.set_policy({"RESUME": ["line.state == 'RUN'"]})
    lax2 = _intent(0, w=1000, pre=())
    rec = rt.process_intent(lax2)
    assert rec.state == IntentState.REVOKED and rec.policy_predicates == ["line.state == 'RUN'"]
    assert "line.state" in rec.read_set


def _committed_store(n: int) -> RedisContextStore:
    import time
    store = RedisContextStore("")
    store.update({"line": {"state": "RUN"}})
    v = read_set_version(store.snapshot_full()[2], ["line.state"])
    for i in range(n):
        assert store.commit_authorization(f"i{i}", ["line.state"], v,
                                          {"read_set": ["line.state"], "read_set_version": v},
                                          time.time() * 1000.0, None)["status"] == "committed"
    return store


def test_consumer_replays_after_crash_without_duplicate_effects():
    store, act = _committed_store(10), IdempotentActuator()

    def crash_once(pos, _rec, state={"done": False}):
        if pos == 4 and not state["done"]:
            state["done"] = True
            raise ConsumerCrash()

    c = ActuationConsumer(store, act, after_apply_hook=crash_once)
    try:
        c.poll()
    except ConsumerCrash:
        pass
    ActuationConsumer(store, act).poll()        # restart from the durable offset
    assert act.effects == [f"i{i}" for i in range(10)] and act.suppressed_duplicates == 1


def test_consumer_flags_post_commit_invalidation():
    store, act = _committed_store(3), IdempotentActuator()
    store.update({"line": {"state": "PAUSED"}})
    c = ActuationConsumer(store, act, recheck_at_apply=True)
    c.poll()
    assert c.invalidated_in_flight == ["i0", "i1", "i2"] and c.withheld == ["i0", "i1", "i2"] and not act.effects
