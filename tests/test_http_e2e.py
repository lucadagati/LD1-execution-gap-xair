"""HTTP integration tests against live XAIR (requires stack running)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("XAIR_INTEGRATION") != "1",
    reason="Set XAIR_INTEGRATION=1 with stack running",
)


def test_metrics_endpoint():
    import urllib.request

    with urllib.request.urlopen("http://127.0.0.1:8080/v1/metrics", timeout=3) as r:
        data = json.loads(r.read())
    assert "intents_received" in data


def test_revoke_on_paused_line():
    import urllib.request

    def post(path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:9092/{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    post("context", {"line": {"state": "PAUSED"}, "robot": {"speed": 0.0}})
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    out = post(
        "intent",
        {
            "id": str(uuid.uuid4()),
            "source": "ai",
            "timestamp_decision": ts,
            "freshness_window_ms": 500,
            "preconditions": [{"expr": "line.state != 'PAUSED'"}],
            "payload": {"action_type": "STOP", "target_entity": "robot_3", "parameters": {}},
        },
    )
    assert out.get("outcome") == "REVOKE"
