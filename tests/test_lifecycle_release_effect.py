"""Authorization, commit, release and effect are distinct lifecycle steps.

Regression tests for the lifecycle of paper Fig. 2: nothing is "executed"
because it was authorized or committed; only an effect report confirms an
effect. Covers the runtime API, the HTTP endpoints, idempotency, and the audit
of rejected reports.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

os.environ.setdefault("REDIS_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from xair.adapters.http_server import app  # noqa: E402
from xair.core.lifecycle import (ALLOWED_TRANSITIONS, RELEASED_STATES, SETTLED_STATES,  # noqa: E402
                                 TERMINAL_STATES, InvalidTransition)
from xair.core.models import ActionIntent, EffectStatus, IntentState, ReleaseDecision  # noqa: E402
from xair.core.runtime import XAIRRuntime  # noqa: E402

S = IntentState
client = TestClient(app)


def _body(**kw) -> dict:
    body = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).isoformat(),
        "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": f"conveyor_{uuid.uuid4().hex[:6]}"},
    }
    body.update(kw)
    return body


def _authorized(rt: XAIRRuntime | None = None):
    rt = rt or XAIRRuntime(context={"line": {"state": "RUN"}})
    rec = rt.process_intent(ActionIntent.from_dict(_body()))
    assert rec.state == S.AUTHORIZED
    return rt, rec


# ------------------------------------------------------------------ FSM shape

def test_no_state_means_executed_and_effects_need_a_release():
    assert "EXECUTED" not in S.__members__
    # only RELEASED (or an unresolved UNKNOWN_EFFECT) can reach an effect state
    for st, nxt in ALLOWED_TRANSITIONS.items():
        if S.EFFECT_CONFIRMED in nxt or S.FAILED in nxt:
            assert st in (S.RELEASED, S.UNKNOWN_EFFECT)
    assert S.RELEASED in ALLOWED_TRANSITIONS[S.AUTHORIZED] and S.RELEASED in ALLOWED_TRANSITIONS[S.COMMITTED]
    assert S.REVOKED not in ALLOWED_TRANSITIONS[S.COMMITTED]           # too late for a supervisory revoke
    assert {S.WITHHELD, S.EFFECT_CONFIRMED, S.FAILED, S.REVOKED, S.EXPIRED} <= TERMINAL_STATES
    assert S.UNKNOWN_EFFECT not in TERMINAL_STATES and S.UNKNOWN_EFFECT in SETTLED_STATES
    assert RELEASED_STATES == {S.RELEASED, S.EFFECT_CONFIRMED, S.FAILED, S.UNKNOWN_EFFECT}


# ------------------------------------------------------------------ runtime API

def test_commit_is_an_authorization_not_a_release():
    rt, rec = _authorized()
    rt.record_commit(rec.intent.id, True, "authorization_committed", seq=7)
    assert rec.state == S.COMMITTED and rec.release_decision is None and rec.commit_seq == 7
    assert rt.get_metrics()["released"] == 0 and rt.get_metrics()["committed"] == 1
    rt.report_release(rec.intent.id, True, "middleware_called")
    assert rec.state == S.RELEASED and rec.release_decision == ReleaseDecision.RELEASE
    rt.report_effect(rec.intent.id, "confirmed", "executed:RUN:3")
    assert rec.state == S.EFFECT_CONFIRMED and rec.effect_status == EffectStatus.CONFIRMED
    states = [e["state"] for e in rt.lifecycle.audit_log if e["intent_id"] == rec.intent.id]
    assert states[-3:] == ["COMMITTED", "RELEASED", "EFFECT_CONFIRMED"]


def test_refused_commit_withholds_and_later_reports_are_rejected_and_audited():
    rt, rec = _authorized()
    rt.record_commit(rec.intent.id, False, "read_set_version_changed_at_commit")
    assert rec.state == S.WITHHELD and rec.release_decision == ReleaseDecision.WITHHOLD
    with pytest.raises(InvalidTransition):
        rt.report_release(rec.intent.id, True, "late_release")
    with pytest.raises(InvalidTransition):
        rt.report_effect(rec.intent.id, "confirmed")
    reasons = [e["reason"] for e in rt.lifecycle.audit_log if e["intent_id"] == rec.intent.id]
    assert any(r.startswith("rejected_report:RELEASED") for r in reasons)
    assert any(r.startswith("rejected_report:EFFECT_CONFIRMED") for r in reasons)


def test_effect_requires_release():
    rt, rec = _authorized()
    with pytest.raises(InvalidTransition):
        rt.report_effect(rec.intent.id, "confirmed")
    rt.record_commit(rec.intent.id, True, "authorization_committed")
    with pytest.raises(InvalidTransition):
        rt.report_effect(rec.intent.id, "confirmed")    # committed is not released
    assert rec.state == S.COMMITTED


def test_controller_refusal_is_a_failed_effect():
    rt, rec = _authorized()
    rt.report_release(rec.intent.id, True, "published_after_gate_recheck")
    rt.report_effect(rec.intent.id, EffectStatus.FAILED, "refused:version_changed:4")
    assert rec.state == S.FAILED and rec.effect_detail == "refused:version_changed:4"


def test_unknown_effect_can_be_resolved_later_and_replays_are_idempotent():
    rt, rec = _authorized()
    rt.report_release(rec.intent.id, True, "published")
    rt.report_effect(rec.intent.id, "unknown", "ros_published_no_feedback")
    assert rec.state == S.UNKNOWN_EFFECT
    rt.report_effect(rec.intent.id, "unknown")           # replay: no change
    rt.report_effect(rec.intent.id, "confirmed", "late_feedback")
    rt.report_effect(rec.intent.id, "confirmed")         # replay: no change
    assert rec.state == S.EFFECT_CONFIRMED
    with pytest.raises(InvalidTransition):
        rt.report_effect(rec.intent.id, "failed")


def test_release_and_commit_replays_are_idempotent_but_conflicts_are_not():
    rt, rec = _authorized()
    rt.record_commit(rec.intent.id, True, "authorization_committed")
    rt.record_commit(rec.intent.id, True, "authorization_committed")
    rt.report_release(rec.intent.id, True, "middleware_called")
    rt.report_release(rec.intent.id, True, "middleware_called")
    with pytest.raises(InvalidTransition):
        rt.report_release(rec.intent.id, False, "conflicting_report")
    assert rt.get_metrics()["released"] == 1 and rec.state == S.RELEASED


def test_release_guard_withholds_a_committed_intent():
    rt, rec = _authorized()
    rt.record_commit(rec.intent.id, True, "authorization_committed")
    rt.report_release(rec.intent.id, False, "withheld_by_release_guard:deadline exceeded")
    assert rec.state == S.WITHHELD


def test_supervisory_revoke_only_before_commit_or_release():
    rt, rec = _authorized()
    rt.supervisory_revoke(rec.intent.id)
    assert rec.state == S.REVOKED
    with pytest.raises(InvalidTransition):
        rt.record_commit(rec.intent.id, True, "authorization_committed")
    rt2, rec2 = _authorized()
    rt2.record_commit(rec2.intent.id, True, "authorization_committed")
    with pytest.raises(InvalidTransition):
        rt2.supervisory_revoke(rec2.intent.id)


def test_target_lock_is_held_until_commit_or_release():
    rt = XAIRRuntime(context={"line": {"state": "RUN"}})
    first = rt.process_intent(ActionIntent.from_dict(_body(payload={"action_type": "RESUME", "target_entity": "c1"})))
    second = rt.process_intent(ActionIntent.from_dict(_body(payload={"action_type": "RESUME", "target_entity": "c1"})))
    assert first.state == S.AUTHORIZED and second.state == S.DELAYED
    rt.record_commit(first.intent.id, True, "authorization_committed")
    third = rt.process_intent(ActionIntent.from_dict(_body(payload={"action_type": "RESUME", "target_entity": "c1"})))
    assert third.state == S.AUTHORIZED


# ------------------------------------------------------------------ HTTP API

def _submit(body: dict) -> dict:
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    return client.post("/v1/intents", json=body).json()


def test_http_commit_returns_committed_not_released():
    body = _body()
    assert _submit(body)["state"] == "AUTHORIZED"
    res = client.post(f"/v1/intents/{body['id']}/commit", json={"scope": "readset"}).json()
    assert res["committed"] is True and res["state"] == "COMMITTED" and "released" not in res
    assert client.get(f"/v1/intents/{body['id']}").json()["state"] == "COMMITTED"
    rel = client.post(f"/v1/intents/{body['id']}/release",
                      json={"released": True, "reason": "middleware_called",
                            "effect_status": "confirmed", "effect_detail": "executed:RUN:1"}).json()
    assert rel["state"] == "EFFECT_CONFIRMED" and rel["release_decision"] == "RELEASE"


def test_http_refused_commit_is_withheld():
    body = _body()
    _submit(body)
    client.post("/v1/context/snapshot", json={"line": {"state": "PAUSED"}})
    res = client.post(f"/v1/intents/{body['id']}/commit", json={"scope": "readset"}).json()
    assert res["committed"] is False and res["state"] == "WITHHELD"
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})


def test_http_withheld_after_commit_closes_the_record():
    body = _body()
    _submit(body)
    client.post(f"/v1/intents/{body['id']}/commit", json={"scope": "readset"})
    res = client.post(f"/v1/intents/{body['id']}/withheld", json={"reason": "withheld_by_release_guard"}).json()
    assert res["withheld"] is True and res["state"] == "WITHHELD"


def test_http_effect_endpoint_and_conflicts():
    body = _body()
    _submit(body)
    assert client.post(f"/v1/intents/{body['id']}/effect", json={"status": "confirmed"}).status_code == 409
    client.post(f"/v1/intents/{body['id']}/release", json={"released": True, "reason": "published"})
    res = client.post(f"/v1/intents/{body['id']}/effect", json={"status": "unknown", "detail": "no_feedback"})
    assert res.json()["state"] == "UNKNOWN_EFFECT"
    assert client.post(f"/v1/intents/{body['id']}/effect", json={"status": "bogus"}).status_code == 422


def test_http_deprecated_publication_alias_maps_to_release():
    body = _body()
    _submit(body)
    res = client.post(f"/v1/intents/{body['id']}/publication", json={"published": False, "reason": "gate"}).json()
    assert res["state"] == "WITHHELD" and res["release_decision"] == "WITHHOLD"
