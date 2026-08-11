#!/usr/bin/env python3
"""E8: ROS and Gazebo motion proof aligned with E1b stale-RESUME semantics."""

from __future__ import annotations

import argparse
import csv
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "http://127.0.0.1:9092"
XAIR = "http://127.0.0.1:8080"
MOTION_FILE = ROOT / "experiments" / "results" / "e8_motion_state.json"
AUDIT_FILE = ROOT / "experiments" / "results" / "ros_audit_state.json"
RESULTS_DIR = ROOT / "experiments" / "results"
BASELINES = ("xair", "direct", "naive", "local")


def audit_count() -> int | None:
    """Pose-message count from the independent ROS audit witness (None if not running)."""
    if not AUDIT_FILE.exists():
        return None
    for _ in range(3):
        try:
            return int(json.loads(AUDIT_FILE.read_text()).get("pose_count", 0))
        except (json.JSONDecodeError, ValueError):
            time.sleep(0.02)
    return None


def http_get(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def post(path: str, body: dict, mode: str | None = None, retries: int = 5) -> dict:
    import urllib.error
    import urllib.request

    url = f"{ADAPTER}/{path}"
    if mode and path == "intent":
        url = f"{url}?mode={mode}"
    payload = json.dumps(body).encode()
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
                if not raw.strip():
                    raise ValueError("empty response body")
                return json.loads(raw)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_err = exc
            time.sleep(0.08 * (attempt + 1))
    return {"outcome": "UNKNOWN", "ros_published": False, "error": str(last_err)}


def get_context() -> dict:
    return http_get(f"{XAIR}/v1/context/snapshot").get("context", {})


def motion_state() -> dict:
    if not MOTION_FILE.exists():
        return {}
    for _ in range(3):
        try:
            return json.loads(MOTION_FILE.read_text())
        except json.JSONDecodeError:
            time.sleep(0.02)
    return {}


def arm_position() -> float | None:
    state = motion_state()
    if "arm_position" in state:
        return float(state["arm_position"])
    lj = state.get("last_joints", {})
    if "arm_slide_joint" in lj:
        return float(lj["arm_slide_joint"])
    return None


def motion_count() -> int:
    return int(motion_state().get("motion_count", 0))


def reset_gazebo_arm() -> None:
    import subprocess

    bash = "source /opt/ros/jazzy/setup.bash && "
    # Retract via the same UE_TCP bridge path used during experiments.
    subprocess.run(
        bash
        + "ros2 topic pub --once /UE_TCP_position geometry_msgs/msg/Pose "
        + "\"{position: {x: 0.05, y: 0.0, z: 0.0}, orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}}\"",
        shell=True,
        executable="/bin/bash",
        capture_output=True,
    )
    for _ in range(30):
        if (arm_position() or 1.0) < 0.35:
            break
        time.sleep(0.1)


def run_single(baseline: str, run_idx: int, pause_ms: float, use_gazebo: bool) -> dict:
    post("context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "robot": {"speed": 0.05}})
    time.sleep(pause_ms / 1000.0)
    post("context", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
    for _ in range(15):
        if get_context().get("line", {}).get("state") == "PAUSED":
            break
        time.sleep(0.02)
        post("context", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
    time.sleep(0.08)

    arm_before = arm_after = None
    motion_before = motion_after = None
    if use_gazebo:
        if (arm_position() or 0) > 0.35:
            reset_gazebo_arm()
        arm_before = arm_position()
        motion_before = motion_count()

    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "run": run_idx},
    }

    audit_before = audit_count()
    resp = post("intent", intent, mode=baseline)
    time.sleep(0.25 if not use_gazebo else 0.9)
    audit_after = audit_count()
    if use_gazebo:
        arm_after = arm_position()
        motion_after = motion_count()

    outcome = resp.get("outcome") or "UNKNOWN"
    ros_published = bool(resp.get("ros_published"))
    # Independent witness: message actually observed on the actuator topic.
    ros_observed = (
        audit_after - audit_before > 0
        if audit_before is not None and audit_after is not None
        else None
    )
    context_obsolete = True
    unknown = outcome == "UNKNOWN" or bool(resp.get("error"))
    stale_executed = ros_published and context_obsolete and not unknown
    stale_observed = bool(ros_observed) and context_obsolete and not unknown
    arm_delta = (
        abs((arm_after or 0) - (arm_before or 0))
        if use_gazebo and arm_before is not None and arm_after is not None
        else 0.0
    )
    motion_delta = (
        (motion_after - motion_before)
        if use_gazebo and motion_before is not None and motion_after is not None
        else 0
    )
    sim_motion = use_gazebo and (motion_delta > 0 or arm_delta > 0.05)

    return {
        "baseline": baseline,
        "run": run_idx,
        "outcome": outcome,
        "unknown": unknown,
        "ros_published": ros_published,
        "ros_observed": ros_observed,
        "stale_executed": stale_executed,
        "stale_observed": stale_observed,
        "witness_agreement": (ros_observed is None) or (ros_observed == ros_published),
        "context_obsolete": context_obsolete,
        "arm_delta": round(arm_delta, 4),
        "sim_motion": sim_motion,
        "error": resp.get("error", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--pause-ms", type=float, default=400)
    parser.add_argument("--baselines", nargs="+", default=list(BASELINES))
    parser.add_argument("--use-gazebo", action="store_true")
    args = parser.parse_args()

    out_csv = RESULTS_DIR / ("e8_gazebo_cell_sim.csv" if args.use_gazebo else "e8_gazebo_cell.csv")
    rows = [run_single(b, i, args.pause_ms, args.use_gazebo) for b in args.baselines for i in range(args.runs)]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    summary = {}
    for b in args.baselines:
        sub = [r for r in rows if r["baseline"] == b]
        n = len(sub)
        unknown = sum(1 for r in sub if str(r["unknown"]).lower() in ("true", "1"))
        stale = sum(1 for r in sub if str(r["stale_executed"]).lower() in ("true", "1"))
        observed = sum(1 for r in sub if str(r.get("stale_observed", False)).lower() in ("true", "1"))
        agree = sum(1 for r in sub if str(r.get("witness_agreement", True)).lower() in ("true", "1"))
        sim = sum(1 for r in sub if str(r.get("sim_motion", False)).lower() in ("true", "1"))
        arms = [float(r["arm_delta"]) for r in sub if float(r.get("arm_delta") or 0) > 0]
        known = n - unknown
        summary[b] = {
            "runs": n,
            "known_runs": known,
            "unknown_rate": unknown / n if n else 0,
            "stale_executed_rate": stale / known if known else 0,
            "stale_observed_rate": observed / known if known else 0,
            "witness_agreement_rate": agree / n if n else 0,
            "stale_executed_rate_all_runs": stale / n if n else 0,
            "sim_motion_rate": sim / n if n else 0,
            "arm_delta_mean": sum(arms) / len(arms) if arms else 0.0,
        }
    print(json.dumps({"csv": str(out_csv), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
