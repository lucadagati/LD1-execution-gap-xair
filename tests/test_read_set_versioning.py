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


def _commit(store, intent_id, expected, *, age_bound=None, decision_ms=None):
    import time as _t
    return store.commit_authorization(intent_id, ["line.state"], expected, {"read_set": ["line.state"]},
                                      decision_epoch_ms=_t.time() * 1000.0 if decision_ms is None else decision_ms,
                                      age_bound_ms=age_bound)


def _stores():
    """In-memory store, plus a Redis one on db 1 when a local server is reachable."""
    import os
    out = [RedisContextStore("")]
    url = os.environ.get("XAIR_TEST_REDIS_URL", "redis://127.0.0.1:6379/1")
    try:
        import redis
        client = redis.from_url(url)
        client.ping()
        client.delete("xair:snapshot", "xair:actuations", "xair:committed")
        out.append(RedisContextStore(url))
    except Exception:
        pass
    return out


def test_atomic_commit_serializes_with_context_updates():
    for store in _stores():
        store.update({"line": {"state": "RUN"}})
        _, _, pv, _ = store.snapshot_full()
        expected = read_set_version(pv, ["line.state"])
        store.update({"telemetry": {"t": 1.0}})  # unrelated write: still committable
        res = _commit(store, "a", expected)
        assert res["status"] == "committed" and res["observed"] == expected and res["seq"] == 1
        assert _commit(store, "a", expected)["status"] == "duplicate"
        store.update({"line": {"state": "PAUSED"}})
        res = _commit(store, "b", expected)
        assert res["status"] == "changed" and res["observed"] != expected
        assert [r["intent_id"] for r in store.actuation_log()] == ["a"]


def test_atomic_commit_checks_age_on_the_store_clock():
    import time as _t
    for store in _stores():
        store.update({"line": {"state": "RUN"}})
        expected = read_set_version(store.snapshot_full()[2], ["line.state"])
        old = _t.time() * 1000.0 - 500.0
        res = _commit(store, "late", expected, age_bound=100.0, decision_ms=old)
        assert res["status"] == "expired" and res["age_at_commit_ms"] >= 500.0
        res = _commit(store, "ok", expected, age_bound=10_000.0, decision_ms=old)
        assert res["status"] == "committed" and store.actuation_log()[-1]["age_at_commit_ms"] >= 500.0


def test_atomic_commit_under_concurrent_writers_never_commits_after_change():
    import threading
    for store in _stores():
        store.update({"line": {"state": "RUN"}})
        expected = read_set_version(store.snapshot_full()[2], ["line.state"])
        results = []

        def writer():
            store.update({"line": {"state": "PAUSED"}})

        def committer(i):
            results.append(_commit(store, f"c{i}", expected))

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=committer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        pause_version = store.snapshot_full()[2]["line.state"]
        # every committed authorization happened at a global version before the write
        assert all(r["commit_version"] < pause_version for r in results if r["status"] == "committed")
        assert all(r["status"] in ("committed", "changed") for r in results)


def test_atomic_commit_refuses_decisions_ahead_of_the_store_clock():
    import time as _t
    for store in _stores():
        store.update({"line": {"state": "RUN"}})
        expected = read_set_version(store.snapshot_full()[2], ["line.state"])
        ahead = _t.time() * 1000.0 + 200.0
        res = store.commit_authorization("ahead", ["line.state"], expected, {"read_set": ["line.state"]},
                                         decision_epoch_ms=ahead, age_bound_ms=1000.0, max_ahead_ms=50.0)
        assert res["status"] == "future_skew"
