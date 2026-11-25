"""Regression tests for three release-path defects found in review:

1. ``margin_ms`` could be negative (or non-finite) and widen the deadline at commit;
2. ``/withheld`` wrote a tombstone to the actuation log for unknown or uncommitted intents;
3. in the optimistic mode, a supervisory revoke accepted after t_v did not stop the gateway.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

from xair.adapters.http_server import app, runtime, store  # noqa: E402

client = TestClient(app)


def _intent(**kw) -> dict:
    body = {"id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": datetime.now(timezone.utc).isoformat(),
            "freshness_window_ms": 5000, "preconditions": [{"expr": "line.state == 'RUN'"}],
            "payload": {"action_type": "RESUME", "target_entity": f"conveyor_{uuid.uuid4().hex[:6]}"}}
    body.update(kw)
    return body


def _authorized(**kw) -> dict:
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    body = _intent(**kw)
    assert client.post("/v1/intents", json=body).json()["state"] == "AUTHORIZED"
    return body


# ------------------------------------------------------------ 1. commit margin

@pytest.mark.parametrize("margin", [-1000, -0.001, "NaN", "Infinity", "-Infinity"])
def test_commit_rejects_negative_or_non_finite_margin(margin):
    body = _authorized()
    res = client.post(f"/v1/intents/{body['id']}/commit", json={"scope": "readset", "margin_ms": margin})
    assert res.status_code == 422
    assert runtime.lifecycle.get(body["id"]).state.value == "AUTHORIZED"   # nothing was committed


def test_commit_rejects_unknown_scope():
    body = _authorized()
    assert client.post(f"/v1/intents/{body['id']}/commit", json={"scope": "global"}).status_code == 422


def test_negative_margin_can_no_longer_bypass_the_deadline():
    # the review's reproduction: deadline 50 ms, wait 100 ms, margin -1000 ms
    body = _authorized(deadline_ms=50)
    time.sleep(0.1)
    assert client.post(f"/v1/intents/{body['id']}/commit", json={"margin_ms": -1000}).status_code == 422
    res = client.post(f"/v1/intents/{body['id']}/commit", json={"margin_ms": 0}).json()
    assert res["committed"] is False and res["status"] == "expired" and res["state"] == "WITHHELD"


def test_positive_margin_tightens_the_deadline():
    body = _authorized(deadline_ms=5000)
    res = client.post(f"/v1/intents/{body['id']}/commit", json={"margin_ms": 10000}).json()
    assert res["committed"] is False and res["status"] == "expired"


# ------------------------------------------------------------ 2. withheld

def _log_len() -> int:
    return len(store.actuation_log(0))


def test_withheld_on_unknown_intent_is_404_and_writes_nothing():
    n = _log_len()
    assert client.post("/v1/intents/nonexistent/withheld", json={"reason": "x"}).status_code == 404
    assert _log_len() == n


def test_withheld_on_uncommitted_intent_is_409_and_writes_nothing():
    body = _authorized()
    n = _log_len()
    assert client.post(f"/v1/intents/{body['id']}/withheld", json={"reason": "x"}).status_code == 409
    assert _log_len() == n and runtime.lifecycle.get(body["id"]).state.value == "AUTHORIZED"


def test_withheld_after_commit_writes_one_tombstone_and_is_idempotent():
    body = _authorized()
    assert client.post(f"/v1/intents/{body['id']}/commit", json={}).json()["committed"] is True
    n = _log_len()
    res = client.post(f"/v1/intents/{body['id']}/withheld", json={"reason": "guard"}).json()
    assert res["state"] == "WITHHELD" and res["duplicate"] is False and _log_len() == n + 1
    tomb = store.actuation_log(n)[0]
    assert tomb == {"intent_id": body["id"], "withheld": True, "reason": "guard"}
    again = client.post(f"/v1/intents/{body['id']}/withheld", json={"reason": "guard"})
    assert again.status_code == 200 and again.json()["duplicate"] is True and _log_len() == n + 1


def test_withheld_after_a_refused_commit_is_409():
    body = _authorized()
    client.post("/v1/context/snapshot", json={"line": {"state": "PAUSED"}})
    assert client.post(f"/v1/intents/{body['id']}/commit", json={}).json()["committed"] is False
    n = _log_len()
    assert client.post(f"/v1/intents/{body['id']}/withheld", json={"reason": "x"}).status_code == 409
    assert _log_len() == n
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})


# ------------------------------------------------------------ 3. supervisory revoke, optimistic mode

class _InProcessXAIR:
    """XAIRHttpClient whose requests go to the in-process app instead of the network."""

    def __new__(cls):
        from xair_http_client import XAIRHttpClient

        class C(XAIRHttpClient):
            def _request(self, method, path, body=None, timeout=15.0):
                res = client.request(method, path, json=body)
                res.raise_for_status()
                return res.json()
        return C()


def test_snapshot_reports_the_intent_state():
    body = _authorized()
    assert client.get("/v1/context/snapshot", params={"intent_id": body["id"]}).json()["intent_state"] == "AUTHORIZED"
    assert client.get("/v1/context/snapshot", params={"intent_id": "nope"}).json()["intent_state"] is None


@pytest.mark.parametrize("scope", ["readset", "truth"])
def test_optimistic_gate_honours_a_supervisory_revoke_after_validation(monkeypatch, scope):
    import actuator_gateway as gw

    xair = _InProcessXAIR()
    submit = xair.submit_intent

    def submit_then_revoke(intent):
        out = submit(intent)
        assert client.delete(f"/v1/intents/{intent['id']}").json()["state"] == "REVOKED"  # after t_v, before t_g
        return out

    monkeypatch.setattr(xair, "submit_intent", submit_then_revoke)
    monkeypatch.setattr(gw, "XAIR", xair)
    monkeypatch.setattr(gw, "_release", lambda data: pytest.fail("a revoked intent reached middleware"))
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    body = _intent()
    out = gw._xair_policy(body, {"version_scope": scope}, atomic=False)
    assert out["gateway_released"] is False and out["reason"] == "intent_revoked_at_gate"
    assert "audit_error" not in out["reason"]
    assert runtime.lifecycle.get(body["id"]).state.value == "REVOKED"


def test_optimistic_gate_still_releases_an_authorized_intent(monkeypatch):
    import actuator_gateway as gw

    monkeypatch.setattr(gw, "XAIR", _InProcessXAIR())
    monkeypatch.setattr(gw, "_release", lambda data: False)        # no ROS: released, no feedback
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    body = _intent()
    out = gw._xair_policy(body, {"version_scope": "readset"}, atomic=False)
    assert out["gateway_released"] is True
    assert runtime.lifecycle.get(body["id"]).state.value == "UNKNOWN_EFFECT"


def test_optimistic_gate_fails_closed_when_xair_does_not_know_the_intent(monkeypatch):
    # e.g. XAIR restarted between t_v and t_g and lost the lifecycle record
    import actuator_gateway as gw

    xair = _InProcessXAIR()
    get_context = xair.get_context

    def snapshot_without_state(predicates=None, intent_id=None):
        doc = get_context(predicates, intent_id)
        doc["intent_state"] = None
        return doc

    monkeypatch.setattr(xair, "get_context", snapshot_without_state)
    monkeypatch.setattr(gw, "XAIR", xair)
    monkeypatch.setattr(gw, "_release", lambda data: pytest.fail("an intent unknown to XAIR reached middleware"))
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    body = _intent()
    out = gw._xair_policy(body, {"version_scope": "readset"}, atomic=False)
    assert out["gateway_released"] is False and out["reason"] == "intent_unknown_at_gate"
    assert runtime.lifecycle.get(body["id"]).state.value == "WITHHELD"
