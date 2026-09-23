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
