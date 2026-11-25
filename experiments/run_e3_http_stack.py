#!/usr/bin/env python3
"""E3: two-producer conflict on one actuator through the XAIR batch endpoint.

An AI planner and an XR teleoperation client submit intents on the same
target in one batch at equal priority. The declared policy
(``priority_then_xr``) is a deterministic tie-break, so this suite is a
regression of that policy, not a concurrency stress test: both intents
arrive in a single request and are resolved sequentially.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from common import RESULTS_DIR, XAIR, http_json, now_iso, write_csv, xair_context


def run_trial(run_idx: int) -> dict:
    xair_context({"line": {"state": "RUN"}})
    ts = now_iso()
    common = {"timestamp_decision": ts, "freshness_window_ms": 500,
              "preconditions": [{"expr": "line.state == 'RUN'"}], "priority": 5}
    ai = {"id": str(uuid.uuid4()), "source": "ai",
          "payload": {"action_type": "SET_SPEED", "target_entity": "robot_arm", "parameters": {"speed": 0.2}}, **common}
    xr = {"id": str(uuid.uuid4()), "source": "xr",
          "payload": {"action_type": "SET_POSE", "target_entity": "robot_arm", "parameters": {}}, **common}
    out = http_json(f"{XAIR}/v1/intents/batch", [ai, xr])
    results = {r["source"]: r for r in out.get("results", [])}
    xr_out = results.get("xr", {}).get("outcome")
    ai_out = results.get("ai", {}).get("outcome")
    return {
        "run": run_idx,
        "winner": out.get("winner"),
        "ai_outcome": ai_out,
        "ai_reason": results.get("ai", {}).get("reason"),
        "xr_outcome": xr_out,
        "xr_wins": int(xr_out == "EXECUTE" and ai_out == "REVOKE"),
        "cv": out.get("cv", 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--out", default=str(RESULTS_DIR / "e3_conflict_http.csv"))
    args = parser.parse_args()

    rows = [run_trial(i) for i in range(args.runs)]
    out = write_csv(Path(args.out), rows)
    print(json.dumps({"CV": sum(int(r["cv"]) for r in rows),
                      "xr_win_rate": sum(r["xr_wins"] for r in rows) / len(rows), "out": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
