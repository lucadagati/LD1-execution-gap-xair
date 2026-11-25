"""AIS contract: what the schema accepts is exactly what the runtime enforces.

* unknown fields (including the former ``revocable``) are rejected, fail closed;
* only implemented degradation policies are accepted, and the degraded payload
  is the one authorized and returned for release;
* a DELAYED intent is revalidated when its id is resubmitted over HTTP.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

os.environ.setdefault("REDIS_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from xair.adapters.http_server import app, runtime  # noqa: E402
from xair.core.models import ActionIntent  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "action-intent-v1.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())
client = TestClient(app)


def _body(**kw) -> dict:
    body = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).isoformat(),
        "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "PICK", "target_entity": f"robot_{uuid.uuid4().hex[:6]}", "parameters": {}},
    }
    body.update(kw)
    return body


@pytest.mark.parametrize("example", sorted((ROOT / "examples").glob("*.json")))
def test_examples_are_valid_ais(example):
    VALIDATOR.validate(json.loads(example.read_text()))


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(revocable=False),                                   # removed from the contract
    lambda b: b.update(unknown_field=1),
    lambda b: b["payload"].update(run=3),                                   # harness tags go in parameters
    lambda b: b["payload"].update(degradation_policy="partial_pose"),
    lambda b: b["payload"].update(degradation_policy="hold_position"),
    lambda b: b["preconditions"].append({"expr": "x == 1", "weight": 2}),
])
def test_schema_rejects_fields_the_runtime_would_not_honour(mutate):
    body = _body()
    mutate(body)
    assert list(VALIDATOR.iter_errors(body))
    res = client.post("/v1/intents", json=body).json()
    assert res["outcome"] == "REVOKE" and res["reason"].startswith("schema_invalid")
    assert runtime.lifecycle.get(body["id"]) is None          # refused before a record is created


@pytest.mark.parametrize("policy", ["partial_pose", "hold_position", "bogus"])
def test_in_process_construction_rejects_unsupported_degradation(policy):
    body = _body()
    body["payload"]["degradation_policy"] = policy
    with pytest.raises(ValueError):
        ActionIntent.from_dict(body)


def test_reduce_speed_is_applied_revalidated_and_returned_for_release():
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    body = _body()
    body["payload"].update(parameters={"speed_factor": 0.8}, degradation_policy="reduce_speed")
    res = client.post("/v1/intents", json=copy.deepcopy(body)).json()
    assert res["state"] == "AUTHORIZED" and res["outcome"] == "EXECUTE"
    assert res["authorized_payload"]["parameters"]["speed_factor"] == pytest.approx(0.4)
    assert res["authorized_payload"]["degradation_policy"] == "none"
    states = [e["state"] for e in runtime.lifecycle.audit_log if e["intent_id"] == body["id"]]
    assert "DEGRADED" in states and states[-1] == "AUTHORIZED"
    assert len(runtime.receiver) == 0                          # nothing left on the internal queue


def test_delayed_intent_is_revalidated_when_resubmitted():
    client.post("/v1/context/snapshot", json={"line": {"state": "RUN"}})
    target = f"robot_{uuid.uuid4().hex[:6]}"
    holder, waiter = _body(), _body()
    holder["payload"]["target_entity"] = waiter["payload"]["target_entity"] = target
    assert client.post("/v1/intents", json=holder).json()["state"] == "AUTHORIZED"
    first = client.post("/v1/intents", json=waiter).json()
    assert first["state"] == "DELAYED" and first["outcome"] == "DELAY"
    client.post(f"/v1/intents/{holder['id']}/release", json={"released": True, "reason": "published"})
    again = client.post("/v1/intents", json=waiter).json()
    assert again["state"] == "AUTHORIZED" and again["duplicate"] is False
    # an authorized id is never revalidated again
    assert client.post("/v1/intents", json=waiter).json()["duplicate"] is True


def test_deadline_semantics_are_documented_as_release_bound():
    desc = SCHEMA["properties"]["deadline_ms"]["description"]
    assert "release" in desc and "does not bound the physical completion" in desc
    assert "hard constraints" in SCHEMA["properties"]["safety_constraints"]["description"].lower()
