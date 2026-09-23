#!/usr/bin/env python3
"""E8-Gazebo: stale RESUME against a headless Gazebo cell with two witnesses.

Requires ROS 2 Jazzy, the Gazebo cell (scripts/start_gazebo_cell.sh), the
ROS audit subscriber and the joint-state motion tracker
(scripts/run_e8_gazebo_full.sh starts all of them).

Two independent observers are recorded per trial:
  * ``ros_observed``  -- the ROS audit subscriber saw a message on the
    actuator topic (message-level witness, never consults the adapter);
  * ``sim_motion``    -- the simulated arm joint moved (physical-effect proxy,
    read from /joint_states by the motion tracker).
They answer different questions and are summarized separately.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

from common import RESULTS_DIR, adapter, audit_count, now_iso, released, wait_xair_state, write_csv

MOTION_FILE = RESULTS_DIR / "e8_motion_state.json"
BASELINES = ("xair", "direct", "naive", "local")


def motion_state() -> dict:
    for _ in range(3):
        try:
            return json.loads(MOTION_FILE.read_text()) if MOTION_FILE.exists() else {}
        except json.JSONDecodeError:
            time.sleep(0.02)
    return {}


def arm_position() -> float | None:
    state = motion_state()
    if "arm_position" in state:
        return float(state["arm_position"])
    lj = state.get("last_joints", {})
    return float(lj["arm_slide_joint"]) if "arm_slide_joint" in lj else None


def motion_count() -> int:
    return int(motion_state().get("motion_count", 0))


def reset_gazebo_arm() -> None:
    subprocess.run(
        "source /opt/ros/jazzy/setup.bash && ros2 topic pub --once /UE_TCP_position geometry_msgs/msg/Pose "
        "\"{position: {x: 0.05, y: 0.0, z: 0.0}, orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}}\"",
        shell=True, executable="/bin/bash", capture_output=True,
    )
    for _ in range(30):
        if (arm_position() or 1.0) < 0.35:
            break
        time.sleep(0.1)


def run_single(baseline: str, run_idx: int, pause_ms: float, settle_s: float) -> dict:
    # Retract the arm *before* t_d: the reset publishes via the ROS CLI and can
    # take over a second, which must not age the intent under test.
    if (arm_position() or 0) > 0.35:
        reset_gazebo_arm()
    time.sleep(settle_s)
    adapter("context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "robot": {"speed": 0.05}})
    intent = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": 1000,
        "preconditions": [{"expr": "line.state == 'RUN'"}, {"expr": "gripper.state == 'OPEN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "robot_3", "run": run_idx},
    }
    t_decision = time.monotonic()
    time.sleep(pause_ms / 1000.0)
    adapter("context", {"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
    wait_xair_state("PAUSED")

    arm_before, motion_before, audit_before = arm_position(), motion_count(), audit_count()
    resp = adapter("intent", intent, mode=baseline)
    age_ms = (time.monotonic() - t_decision) * 1000.0
    time.sleep(settle_s)
    arm_after, motion_after, audit_after = arm_position(), motion_count(), audit_count()

    rel = released(resp)
    ros_observed = (audit_after - audit_before > 0) if audit_before is not None and audit_after is not None else None
    arm_delta = abs((arm_after or 0) - (arm_before or 0)) if arm_before is not None and arm_after is not None else 0.0
    sim_motion = (motion_after - motion_before) > 0 or arm_delta > 0.05
    return {
        "baseline": baseline,
        "run": run_idx,
        "outcome": resp.get("outcome") or "UNKNOWN",
        "reason": resp.get("reason"),
        "gateway_released": int(rel),
        "stale_executed": int(rel),
        "ros_published": int(bool(resp.get("ros_published"))),
        "ros_observed": "" if ros_observed is None else int(ros_observed),
        "ros_witness_agrees": "" if ros_observed is None else int(ros_observed == rel),
        "arm_delta": round(arm_delta, 4),
        "sim_motion": int(sim_motion),
        "motion_witness_agrees": int(sim_motion == rel),
        "error": resp.get("error", ""),
        "intent_age_at_submit_ms": round(age_ms, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--pause-ms", type=float, default=200)
    parser.add_argument("--settle-s", type=float, default=0.9)
    parser.add_argument("--baselines", nargs="+", default=list(BASELINES))
    parser.add_argument("--campaign", default="1", help="Tag stored in the output file name; never overwrite a campaign")
    args = parser.parse_args()

    rows = [run_single(b, i, args.pause_ms, args.settle_s) for b in args.baselines for i in range(args.runs)]
    for r in rows:
        r["campaign"] = args.campaign
    out = write_csv(Path(RESULTS_DIR / f"e8_gazebo_campaign{args.campaign}.csv"), rows)
    print(f"Wrote {len(rows)} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
