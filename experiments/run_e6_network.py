#!/usr/bin/env python3
"""E6: network emulation via tc netem on loopback (requires sudo/cap_net_admin)."""

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

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
RESULTS = ROOT / "experiments" / "results" / "e6_network.csv"

NETEM_CONFIGS = [
    {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0},
    {"delay_ms": 10, "jitter_ms": 0, "loss_pct": 0},
    {"delay_ms": 50, "jitter_ms": 10, "loss_pct": 0},
    {"delay_ms": 100, "jitter_ms": 10, "loss_pct": 0},
    {"delay_ms": 250, "jitter_ms": 50, "loss_pct": 0},
    {"delay_ms": 50, "jitter_ms": 0, "loss_pct": 0.1},
    {"delay_ms": 50, "jitter_ms": 0, "loss_pct": 1.0},
]


def tc_apply(delay_ms: float, jitter_ms: float, loss_pct: float) -> bool:
    subprocess.run(["sudo", "tc", "qdisc", "del", "dev", "lo", "root"], capture_output=True)
    if delay_ms <= 0 and loss_pct <= 0:
        return True
    netem = f"delay {delay_ms}ms"
    if jitter_ms > 0:
        netem += f" {jitter_ms}ms"
    if loss_pct > 0:
        netem += f" loss {loss_pct}%"
    r = subprocess.run(["sudo", "tc", "qdisc", "add", "dev", "lo", "root", "netem"] + netem.split(),
                       capture_output=True, text=True)
    return r.returncode == 0


def tc_clear() -> None:
    subprocess.run(["sudo", "tc", "qdisc", "del", "dev", "lo", "root"], capture_output=True)


def post(path: str, body: dict, mode: str = "xair") -> dict:
    import urllib.request

    url = f"{ADAPTER}/{path}?mode={mode}" if path == "intent" else f"{ADAPTER}/{path}"
    t0 = time.perf_counter()
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode())
    out["e2e_latency_ms"] = (time.perf_counter() - t0) * 1000.0
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--use-netem", action="store_true", help="Apply tc netem (needs sudo)")
    args = parser.parse_args()

    rows = []
    netem_ok = args.use_netem and tc_apply(0, 0, 0) or not args.use_netem
    if args.use_netem and not netem_ok:
        print("WARN: tc netem unavailable — falling back to client-side delay only")

    try:
        for cfg in NETEM_CONFIGS:
            if args.use_netem:
                tc_apply(cfg["delay_ms"], cfg["jitter_ms"], cfg["loss_pct"])
            for i in range(args.runs):
                post("context", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
                intent = {
                    "id": str(uuid.uuid4()),
                    "source": "ai",
                    "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "freshness_window_ms": 500,
                    "preconditions": [{"expr": "line.state == 'RUN'"}],
                    "payload": {"action_type": "RESUME", "target_entity": "line_1"},
                }
                if not args.use_netem and cfg["delay_ms"] > 0:
                    time.sleep(cfg["delay_ms"] / 1000.0)
                resp = post("intent", intent)
                rows.append({
                    **cfg,
                    "run": i,
                    "netem_applied": args.use_netem,
                    "outcome": resp.get("outcome"),
                    "ros_published": resp.get("ros_published", False),
                    "e2e_latency_ms": resp.get("e2e_latency_ms", 0),
                })
    finally:
        tc_clear()

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
