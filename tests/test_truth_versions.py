"""Truth-change predicate versions and the commit endpoint, in process (in-memory store)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

os.environ.setdefault("REDIS_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from xair.adapters.http_server import app  # noqa: E402
from xair.core.context_store import RedisContextStore  # noqa: E402
from xair.core.versioning import predicate_version  # noqa: E402

client = TestClient(app)


def _intent(pre, action="RESUME"):
    return {"id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": datetime.now(timezone.utc).isoformat(),
            "freshness_window_ms": 5000, "preconditions": [{"expr": e} for e in pre],
            "payload": {"action_type": action, "target_entity": f"t_{uuid.uuid4().hex[:6]}"}}


def test_truth_version_ignores_value_changes_that_keep_the_predicate_true():
    s = RedisContextStore("")
    s.update({"p": {"x": 100.0}})
    v0 = s.register_predicates(["p.x < 250"])["p.x < 250"][0]
    for x in (120.0, 180.0, 90.0):
        s.update({"p": {"x": x}})
    assert predicate_version(s.predicate_versions, ["p.x < 250"]) == v0


def test_truth_version_detects_a_change_undone_before_the_gate():
    s = RedisContextStore("")
    s.update({"line": {"state": "RUN"}})
    v0 = s.register_predicates(["line.state == 'RUN'"])["line.state == 'RUN'"][0]
    s.update({"line": {"state": "PAUSED"}})
    s.update({"line": {"state": "RUN"}})   # A-B-A: true again, but it was false in between
    assert predicate_version(s.predicate_versions, ["line.state == 'RUN'"]) > v0


def test_commit_with_truth_scope_over_http():
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}, "p": {"x": 100.0}})
    body = _intent(["line.state == 'RUN'", "p.x < 250"])
    out = client.post("/v1/intents", json=body).json()
    assert out["outcome"] == "EXECUTE" and out["predicate_version"] >= 0
    client.post("/v1/context/snapshot", json={"p": {"x": 150.0}})       # value change, predicate still true
    res = client.post(f"/v1/intents/{body['id']}/commit", json={"scope": "truth"}).json()
    assert res["status"] == "committed"
    body2 = _intent(["line.state == 'RUN'"])
    assert client.post("/v1/intents", json=body2).json()["outcome"] == "EXECUTE"
    client.post("/v1/context/snapshot", json={"line": {"state": "PAUSED"}})
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    res2 = client.post(f"/v1/intents/{body2['id']}/commit", json={"scope": "truth"}).json()
    assert res2["status"] == "changed"


def test_policy_endpoint_makes_predicates_mandatory():
    client.post("/v1/context/snapshot", json={"line": {"state": "PAUSED"}, "gripper": {"state": "OPEN"}})
    client.put("/v1/policy", json={"RESUME": ["line.state == 'RUN'"]})
    try:
        out = client.post("/v1/intents", json=_intent(["gripper.state == 'OPEN'"])).json()
        assert out["outcome"] == "REVOKE" and out["policy_predicates"] == ["line.state == 'RUN'"]
    finally:
        client.put("/v1/policy", json={})


def test_policy_matches_action_type_regardless_of_case():
    client.put("/v1/policy", json={"resume": ["line.state == 'RUN'"]})
    try:
        client.post("/v1/context/snapshot", json={"line": {"state": "PAUSED"}, "gripper": {"state": "OPEN"}})
        r = client.post("/v1/intents", json=_intent(["gripper.state == 'OPEN'"], action="Resume")).json()
        assert r["outcome"] == "REVOKE" and r["policy_predicates"] == ["line.state == 'RUN'"]
    finally:
        client.put("/v1/policy", json={})

def test_unused_predicates_are_retired_and_fail_closed(monkeypatch):
    import time as _t
    import xair.core.context_store as cs
    s = RedisContextStore("")
    s.update({"line": {"state": "RUN"}})
    s.register_predicates(["line.state == 'RUN'"])
    monkeypatch.setattr(cs, "PREDICATE_RETENTION_S", 0.05)
    _t.sleep(0.1)
    s.update({"other": {"x": 1}})           # any write retires expired registrations
    assert "line.state == 'RUN'" not in s.predicate_versions
    assert predicate_version(s.predicate_versions, ["line.state == 'RUN'"]) == -1


def test_hash_layout_matches_document_layout_on_redis(monkeypatch):
    import redis as _redis
    import xair.core.context_store as cs
    url = os.environ.get("XAIR_TEST_REDIS", "redis://127.0.0.1:6379/1")
    try:
        _redis.from_url(url).ping()
    except Exception:
        import pytest
        pytest.skip("no local redis")
    monkeypatch.setattr(cs, "PREDICATE_LAYOUT", "hash")
    c = _redis.from_url(url)
    c.delete(cs.SNAPSHOT_KEY, cs.PRED_KEY, *c.keys(cs.PRED_ROOT_PREFIX + "*"), cs.ACTUATION_LOG_KEY, cs.COMMITTED_KEY)
    s = cs.RedisContextStore(url)
    assert s._hash_layout
    s.update({"line": {"state": "RUN"}, "p": {"x": 100.0}})
    e1, e2 = "line.state == 'RUN'", "p.x < 250"
    v = s.register_predicates([e1, e2])
    s.update({"p": {"x": 150.0}})                       # truth unchanged
    assert s.snapshot_with_predicates([e2])[5][e2][0] == v[e2][0]
    s.update({"line": {"state": "PAUSED"}}); s.update({"line": {"state": "RUN"}})   # A-B-A
    assert s.snapshot_with_predicates([e1])[5][e1][0] > v[e1][0]
    import time as _t
    res = s.commit_authorization("h1", [e2], v[e2][0], {"read_set": ["p.x"]}, _t.time() * 1000, None, scope="truth")
    assert res["status"] == "committed"
    res = s.commit_authorization("h2", [e1], v[e1][0], {"read_set": ["line.state"]}, _t.time() * 1000, None, scope="truth")
    assert res["status"] == "changed"
    monkeypatch.setattr(cs, "PREDICATE_RETENTION_S", -1.0)
    assert s.gc_predicates() == 2 and c.hlen(cs.PRED_KEY) == 0


def test_shard_layout_matches_document_layout_on_redis(monkeypatch):
    import redis as _redis
    import time as _t
    import xair.core.context_store as cs
    url = os.environ.get("XAIR_TEST_REDIS", "redis://127.0.0.1:6379/1")
    try:
        _redis.from_url(url).ping()
    except Exception:
        import pytest
        pytest.skip("no local redis")
    monkeypatch.setattr(cs, "PREDICATE_LAYOUT", "shard")
    c = _redis.from_url(url)
    c.delete(cs.VER_KEY, cs.CTX_KEY, cs.PRED_KEY, *c.keys(cs.PRED_ROOT_PREFIX + "*"), cs.ACTUATION_LOG_KEY,
             cs.COMMITTED_KEY)
    s = cs.RedisContextStore(url)
    assert s._shard
    s.update({"line": {"state": "RUN"}, "p": {"x": 100.0}, "q": {"y": 1}})
    e1, e2 = "line.state == 'RUN'", "p.x < 250"
    v = s.register_predicates([e1, e2])
    s.update({"p": {"x": 150.0}})                       # truth unchanged
    ctx, ver, pv, _tr, _io, preds = s.snapshot_with_predicates([e2])
    assert preds[e2][0] == v[e2][0] and set(ctx) == {"p"} and pv["p.x"] == ver   # only the root the predicate reads
    s.update({"line": {"state": "PAUSED"}}); s.update({"line": {"state": "RUN"}})   # A-B-A
    assert s.snapshot_with_predicates([e1])[5][e1][0] > v[e1][0]
    res = s.commit_authorization("s1", [e2], v[e2][0], {"read_set": ["p.x"]}, _t.time() * 1000, None, scope="truth")
    assert res["status"] == "committed"
    res = s.commit_authorization("s2", [e1], v[e1][0], {"read_set": ["line.state"]}, _t.time() * 1000, None, scope="truth")
    assert res["status"] == "changed"
    # read-set scope: the version of the paths comes from the root entries
    obs = s.commit_authorization("s3", ["p.x"], -1, {"read_set": ["p.x"]}, _t.time() * 1000, None)
    assert obs["status"] == "changed" and obs["observed"] == pv["p.x"]
    ok = s.commit_authorization("s4", ["p.x"], pv["p.x"], {"read_set": ["p.x"]}, _t.time() * 1000, None)
    assert ok["status"] == "committed"
    full, fver, fpv, trusted = s.snapshot_full()
    assert full["q"] == {"y": 1} and full["p"]["x"] == 150.0 and fver == s.version and trusted
    monkeypatch.setattr(cs, "PREDICATE_RETENTION_S", -1.0)
    assert s.gc_predicates() == 2 and c.hlen(cs.PRED_KEY) == 0
