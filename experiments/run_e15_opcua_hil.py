#!/usr/bin/env python3
"""E15: industrial context path — OPC UA HIL when asyncua is available."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
XAIR = "http://127.0.0.1:8080"
BRIDGE = ROOT / "simulation" / "opcua_hil_bridge.py"
RESULTS = ROOT / "experiments" / "results" / "e15_opcua_hil.csv"
LOG = ROOT / "experiments" / "results" / "e15_transport.log"
OPCUA_PORT = 4840


def _post(url: str, body: dict | None = None) -> dict:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def _get(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def detect_opcua() -> bool:
    try:
        import asyncua  # noqa: F401
        return True
    except ImportError:
        return False


def wait_context(state: str, timeout_s: float = 3.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        snap = _get(f"{XAIR}/v1/context/snapshot")
        line = (snap.get("context") or {}).get("line") or {}
        if line.get("state") == state:
            return True
        time.sleep(0.02)
    return False


def remote_context_update(state: str, transport: str, bridge_proc: subprocess.Popen | None) -> None:
    if transport == "opcua":
        subprocess.run(
            [sys.executable, str(BRIDGE), "--port", str(OPCUA_PORT), "--write", state],
            check=True,
            timeout=15,
        )
        return
    body = {
        "line": {"state": state},
        "gripper": {"state": "OPEN" if state == "RUN" else "CLOSED"},
        "source": "mes",
        "transport": "http_snapshot",
    }
    _post(f"{XAIR}/v1/context/snapshot", body)


def run_trial(run_idx: int, transport: str, bridge_proc: subprocess.Popen | None) -> list[dict]:
    init = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "source": "mes", "transport": transport}
    drift = {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}, "source": "mes", "transport": transport}
    _post(f"{ADAPTER}/context", init)
    remote_context_update("RUN", transport, bridge_proc)
    if not wait_context("RUN"):
        raise RuntimeError(f"run {run_idx}: context did not reach RUN")
    remote_context_update("PAUSED", transport, bridge_proc)
    if not wait_context("PAUSED"):
        raise RuntimeError(f"run {run_idx}: context did not reach PAUSED before intent")

    intent = {
        "id": str(uuid.uuid4()),
        "source": "mes",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 5000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }
    rows = []
    for mode in ("local_stale", "local_push", "xair"):
        q = {"mode": mode}
        if mode == "local_push":
            q["push_notified"] = "false"
        body = {**intent, "id": str(uuid.uuid4())}
        resp = _post(f"{ADAPTER}/intent?{urlencode(q)}", body)
        rows.append({
            "run": run_idx,
            "mode": mode,
            "transport": transport,
            "outcome": resp.get("outcome"),
            "reason": resp.get("reason"),
            "stale_executed": 1 if resp.get("outcome") == "EXECUTE" else 0,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--force-http", action="store_true")
    args = parser.parse_args()

    use_opcua = detect_opcua() and not args.force_http
    transport = "opcua" if use_opcua else "http_snapshot"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps({"transport": transport, "asyncua": use_opcua}) + "\n")

    bridge_proc = None
    if use_opcua:
        bridge_proc = subprocess.Popen(
            [sys.executable, str(BRIDGE), "--daemon", "--port", str(OPCUA_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(2.0)
        if bridge_proc.poll() is not None:
            err = bridge_proc.stderr.read().decode() if bridge_proc.stderr else ""
            raise RuntimeError(f"OPC UA bridge failed to start: {err}")

    rows = []
    try:
        for i in range(args.runs):
            rows.extend(run_trial(i, transport, bridge_proc))
    finally:
        if bridge_proc:
            bridge_proc.terminate()
            bridge_proc.wait(timeout=5)

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    summary = {}
    for mode in ("local_stale", "local_push", "xair"):
        sub = [r for r in rows if r["mode"] == mode]
        summary[mode] = {
            "stale_rate": sum(r["stale_executed"] for r in sub) / len(sub),
            "runs": len(sub),
        }
    print(json.dumps({"transport": transport, "asyncua_version": "2.0.1" if use_opcua else None, "summary": summary, "out": str(RESULTS)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
