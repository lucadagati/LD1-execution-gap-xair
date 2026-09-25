"""Read-set versioning: only value changes on paths an intent reads invalidate it at t_g."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from xair.core.context_store import RedisContextStore
from xair.core.models import ActionIntent
from xair.core.runtime import XAIRRuntime
from xair.core.versioning import changed_paths, read_set, read_set_version


def _intent() -> ActionIntent:
    return ActionIntent.from_dict({
        "id": str(uuid.uuid4()), "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).isoformat(),
        "freshness_window_ms": 1000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    })


def test_read_set_extracts_paths():
    assert read_set(["line.state == 'RUN'", "robot.speed < 0.1", "line.state != 'X'"]) == ["line.state", "robot.speed"]


def test_changed_paths_ignores_same_value_rewrites():
    assert changed_paths({"line": {"state": "RUN"}}, {"line": {"state": "RUN"}, "t": {"c": 21}}) == ["t.c"]


def test_related_paths_count_ancestors_and_descendants():
    pv = {"line.state": 3, "room.temperature": 9, "line": 1}
    assert read_set_version(pv, ["line.state"]) == 3
    assert read_set_version(pv, ["line"]) == 3
    assert read_set_version(pv, ["gripper.state"]) == 0


def test_store_bumps_path_versions_only_on_value_change():
    store = RedisContextStore("")
    store.update({"line": {"state": "RUN"}})
    store.update({"room": {"temperature": 21.0}})
    store.update({"line": {"state": "RUN"}})
    _, version, pv, _ = store.snapshot_full()
    assert version == 3
    assert pv == {"line.state": 1, "room.temperature": 2}


def test_unrelated_update_does_not_block_read_set_gate_but_blocks_global():
    store = RedisContextStore("")
    store.update({"line": {"state": "RUN"}})
    ctx, ver, pv, _ = store.snapshot_full()
    rt = XAIRRuntime()
    rec = rt.process_intent(_intent(), context=ctx, context_version=ver, path_versions=pv)
    store.update({"room": {"temperature": 25.0}})
    _, ver2, pv2, _ = store.snapshot_full()
    assert read_set_version(pv2, rec.read_set) == rec.read_set_version
    assert ver2 != rec.context_version
    final = rt.confirm_publication(rec.intent.id, True, "gate_passed",
                                   read_set_version=read_set_version(pv2, rec.read_set))
    assert final.publication_decision == "PUBLISH"


def test_aba_change_on_read_path_blocks_read_set_gate():
    store = RedisContextStore("")
    store.update({"line": {"state": "RUN"}})
    ctx, ver, pv, _ = store.snapshot_full()
    rt = XAIRRuntime()
    rec = rt.process_intent(_intent(), context=ctx, context_version=ver, path_versions=pv)
    store.update({"line": {"state": "PAUSED"}})
    store.update({"line": {"state": "RUN"}})
    _, _, pv2, _ = store.snapshot_full()
    final = rt.confirm_publication(rec.intent.id, True, "gate_passed",
                                   read_set_version=read_set_version(pv2, rec.read_set))
    assert final.publication_decision == "BLOCK"
    assert final.reason == "read_set_version_changed_at_gate"


def test_atomic_actuation_serializes_with_context_updates():
    store = RedisContextStore("")
    store.update({"line": {"state": "RUN"}})
    _, _, pv, _ = store.snapshot_full()
    expected = read_set_version(pv, ["line.state"])
    store.update({"telemetry": {"t": 1.0}})  # unrelated write: still committable
    ok, observed, _ = store.compare_and_actuate(["line.state"], expected, {"intent_id": "a"})
    assert ok and observed == expected
    store.update({"line": {"state": "PAUSED"}})
    ok, observed, _ = store.compare_and_actuate(["line.state"], expected, {"intent_id": "b"})
    assert not ok and observed != expected
    assert [r["intent_id"] for r in store.actuation_log()] == ["a"]


def test_atomic_actuation_under_concurrent_writers_never_commits_after_change():
    import threading
    store = RedisContextStore("")
    store.update({"line": {"state": "RUN"}})
    _, _, pv, _ = store.snapshot_full()
    expected = read_set_version(pv, ["line.state"])
    results = []

    def writer():
        store.update({"line": {"state": "PAUSED"}})

    def actuator(i):
        results.append(store.compare_and_actuate(["line.state"], expected, {"intent_id": str(i)}))

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=actuator, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pause_version = store.snapshot_full()[2]["line.state"]
    # every committed actuation happened at a global version before the write
    assert all(commit < pause_version for ok, _, commit in results if ok)
