"""Server-enforced action classes: a stop is never revoked, and only the operator assigns classes."""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

from xair.adapters import http_server  # noqa: E402
from xair.adapters.http_server import app, runtime  # noqa: E402

client = TestClient(app)
POLICY = {"RESUME": {"class": "start", "require": ["line.state == 'RUN'"]}, "STOP": {"class": "stop"},
          "class:reset": ["operator.present == true"], "RESET_FAULT": {"class": "reset"}}


def _intent(action="STOP", pre=(), target=None, age_ms=0, **kw):
    t_d = datetime.now(timezone.utc) - timedelta(milliseconds=age_ms)
    return {"id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": t_d.isoformat(),
            "freshness_window_ms": 1000, "preconditions": [{"expr": e} for e in pre],
            "payload": {"action_type": action, "target_entity": target or f"t_{uuid.uuid4().hex[:6]}"}, **kw}


@pytest.fixture(autouse=True)
def _policy():
    client.put("/v1/policy", json=POLICY)
    client.post("/v1/context/snapshot", json={"line": {"state": "PAUSED"}, "operator": {"present": False}})
    yield
    client.put("/v1/policy", json={})


def test_stop_is_authorized_whatever_its_preconditions_and_age():
    out = client.post("/v1/intents", json=_intent(pre=["line.state == 'RUN'"], age_ms=60_000)).json()
    assert out["outcome"] == "EXECUTE" and out["action_class"] == "stop"
    assert out["reason"] == "risk_reducing_action" and out["read_set"] == []


def test_stop_is_authorized_while_the_store_is_unreachable(monkeypatch):
    monkeypatch.setattr(http_server.store, "register_and_snapshot",
                        lambda exprs: {"trusted": False, "version": 0})
    assert client.post("/v1/intents", json=_intent("RESUME")).json()["reason"] == "context_store_untrusted"
    assert client.post("/v1/intents", json=_intent()).json()["outcome"] == "EXECUTE"


def test_stop_takes_no_target_lock_and_is_never_delayed():
    target = "conveyor_1"
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    first = client.post("/v1/intents", json=_intent("RESUME", target=target)).json()
    assert first["outcome"] == "EXECUTE"                       # holds the target until released
    assert client.post("/v1/intents", json=_intent(target=target)).json()["outcome"] == "EXECUTE"
    client.post(f"/v1/intents/{first['id']}/release", json={"released": False, "reason": "test"})


def test_a_stop_preempts_other_intents_on_its_target_in_a_batch():
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    resume = _intent("RESUME", target="cell_7", priority=9)
    stops = [_intent(target="cell_7"), _intent(target="cell_7")]
    res = {r["id"]: r for r in client.post("/v1/intents/batch", json=[resume, *stops]).json()["results"]}
    assert all(res[s["id"]]["outcome"] == "EXECUTE" for s in stops)   # no tie-break among stops
    assert res[resume["id"]]["reason"] == "preempted_by_stop"


def test_producer_cannot_classify_its_own_action():
    body = _intent("RESUME")
    body["action_class"] = "stop"
    assert client.post("/v1/intents", json=body).json()["reason"].startswith("schema_invalid")
    out = client.post("/v1/intents", json=_intent("RESUME", pre=[])).json()
    assert out["action_class"] == "start" and out["outcome"] == "REVOKE"   # mandatory predicate, line paused


def test_class_wide_predicates_apply_to_every_action_of_the_class():
    out = client.post("/v1/intents", json=_intent("RESET_FAULT")).json()
    assert out["policy_predicates"] == ["operator.present == true"] and out["outcome"] == "REVOKE"


@pytest.mark.parametrize("bad", [{"STOP": {"class": "stop", "require": ["line.state == 'RUN'"]}},
                                 {"class:stop": ["line.state == 'RUN'"]}, {"X": {"class": "panic"}},
                                 {"X": {"klass": "stop"}}])
def test_policy_rejects_predicates_on_stops_and_unknown_classes(bad):
    assert client.put("/v1/policy", json=bad).status_code == 422
    assert client.get("/v1/policy").json()["action_classes"]["STOP"] == "stop"     # previous policy kept


def test_policy_changes_require_the_operator_token_when_one_is_set(monkeypatch):
    monkeypatch.setenv("XAIR_POLICY_TOKEN", "s3cret")
    assert client.put("/v1/policy", json={}).status_code == 401
    assert client.put("/v1/policy", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.put("/v1/policy", json=POLICY, headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_gateway_releases_a_stop_without_recheck(monkeypatch):
    import actuator_gateway as gw
    from test_http_hardening import _InProcessXAIR

    released = []
    monkeypatch.setattr(gw, "XAIR", _InProcessXAIR())
    monkeypatch.setattr(gw, "_release", lambda data: released.append(data) or False)
    for atomic in (False, True):
        body = _intent(pre=["line.state == 'RUN'"])
        out = gw._xair_policy(body, {"version_scope": "readset"}, atomic=atomic)
        assert out["gateway_released"] is True and out["reason"] == "released_risk_reducing_action"
        assert runtime.lifecycle.get(body["id"]).state.value == "UNKNOWN_EFFECT"
    assert len(released) == 2
