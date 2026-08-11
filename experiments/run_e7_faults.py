#!/usr/bin/env python3
"""E7: fault injection — duplicate ID, clock skew, stale context, Redis down, adapter restart."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT.parent / "scripts"
XAIR = "http://127.0.0.1:8080"
ADAPTER = "http://127.0.0.1:9092"
RESULTS = ROOT / "experiments" / "results" / "e7_faults.json"


def req(url: str, body: dict | None = None, method: str = "POST") -> dict:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def intent_body(**kw) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }
    base.update(kw)
    return base


def main() -> int:
    results = []

    # 1. Duplicate intent.id (idempotency)
    iid = str(uuid.uuid4())
    intent = intent_body(id=iid)
    req(f"{ADAPTER}/context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    r1 = req(f"{ADAPTER}/intent?mode=xair", intent)
    r2 = req(f"{ADAPTER}/intent?mode=xair", intent)
    results.append({"fault": "duplicate_id", "pass": r1.get("outcome") == r2.get("outcome"), "r1": r1, "r2": r2})

    # 2. Malformed AIS payload (missing required fields)
    import urllib.request
    bad_req = urllib.request.Request(
        f"{ADAPTER}/intent?mode=xair",
        data=b'{"id":"not-a-valid-ais"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(bad_req, timeout=10) as resp:
            malformed = json.loads(resp.read().decode())
    except Exception as e:
        malformed = {"ok": False, "error": str(e)}
    results.append({
        "fault": "malformed_payload",
        "pass": malformed.get("outcome") in (None, "REVOKE", "UNKNOWN") or malformed.get("ok") is False,
        "response": malformed,
    })

    # 3. Clock skew (+500ms future)
    future = (datetime.now(timezone.utc) + timedelta(milliseconds=500)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    r3 = req(f"{ADAPTER}/intent?mode=xair", intent_body(timestamp_decision=future))
    results.append({"fault": "clock_skew_future", "pass": r3.get("outcome") is not None, "response": r3})

    # 4. Stale context → REVOKE
    req(f"{ADAPTER}/context", {"line": {"state": "PAUSED"}})
    time.sleep(0.05)
    r4 = req(f"{ADAPTER}/intent?mode=xair", intent_body())
    results.append({
        "fault": "stale_context",
        "pass": r4.get("outcome") == "REVOKE" and not r4.get("ros_published"),
        "response": r4,
    })

    # 5. Redis down
    subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "stop", "redis"],
                   capture_output=True, cwd=str(ROOT))
    time.sleep(1)
    req(f"{ADAPTER}/context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    req(f"{ADAPTER}/context", {"line": {"state": "PAUSED"}})
    time.sleep(0.05)
    r5 = req(f"{ADAPTER}/intent?mode=xair", intent_body())
    subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "start", "redis"],
                     capture_output=True, cwd=str(ROOT))
    time.sleep(1)
    results.append({
        "fault": "redis_down",
        "pass": r5.get("outcome") in ("REVOKE", "DELAY", "EXECUTE"),
        "response": r5,
    })

    # 6. Adapter restart mid-flight
    subprocess.run(["pkill", "-f", "adaptix_quest_adapter.py"], capture_output=True)
    time.sleep(1)
    subprocess.Popen(
        ["python3", str(SCRIPTS / "adaptix_quest_adapter.py")],
        env={**os.environ, "XAIR_URL": XAIR},
        stdout=open(SCRIPTS.parent / ".run" / "adapter_e7.log", "w"),
        stderr=subprocess.STDOUT,
        cwd=str(SCRIPTS),
    )
    for _ in range(10):
        time.sleep(0.5)
        h = req(f"{ADAPTER}/health", method="GET")
        if h.get("xair"):
            break
    r6 = req(f"{ADAPTER}/health", method="GET")
    req(f"{ADAPTER}/context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    r7 = req(f"{ADAPTER}/intent?mode=xair", intent_body())
    results.append({
        "fault": "adapter_restart",
        "pass": r6.get("xair") is True and r7.get("outcome") is not None and "error" not in r7,
        "health": r6,
        "response": r7,
    })

    # 7. ROS publish count stable on REVOKE
    h0 = req(f"{ADAPTER}/health", method="GET")
    req(f"{ADAPTER}/context", {"line": {"state": "PAUSED"}})
    r8 = req(f"{ADAPTER}/intent?mode=xair", intent_body())
    h1 = req(f"{ADAPTER}/health", method="GET")
    delta = int(h1.get("ros_publish_count", 0)) - int(h0.get("ros_publish_count", 0))
    results.append({
        "fault": "ros_no_publish_on_revoke",
        "pass": r8.get("outcome") == "REVOKE" and delta == 0,
        "ros_delta": delta,
        "response": r8,
    })

    passed = sum(1 for r in results if r.get("pass"))
    out = {"passed": passed, "total": len(results), "cases": results}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
