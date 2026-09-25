#!/usr/bin/env python3
"""E17: a producer that omits a required precondition, with and without server-side policy.

AIS lets producers declare their own preconditions, so a faulty producer can
leave out the one that matters. Ground truth here is the operator's rule that a
RESUME is admissible only while the line runs. Each drift trial builds a RESUME
that declares only the gripper state, pauses the line through the shared
snapshot, and submits through the XAIR gateway. Without policy the gate checks
exactly what the producer declared; with policy XAIR adds the operator's
mandatory predicate for the action type before validation. Valid controls
(line running) measure the false-positive cost of the policy.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from common import RESULTS_DIR, XAIR, adapter, http_json, now_iso, released, wilson_ci, write_csv, xair_context

RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}
POLICY = {"RESUME": ["line.state == 'RUN'"]}


def lax_intent() -> dict:
    return {
        "id": str(uuid.uuid4()), "source": "ai", "timestamp_decision": now_iso(), "freshness_window_ms": 2000,
        "preconditions": [{"expr": "gripper.state == 'OPEN'"}],  # omits line.state == 'RUN'
        "payload": {"action_type": "RESUME", "target_entity": f"e17_{uuid.uuid4().hex[:8]}"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--out", default=str(RESULTS_DIR / "e17_policy.csv"))
    args = ap.parse_args()
    rows = []
    for policy_on in (False, True):
        http_json(f"{XAIR}/v1/policy", POLICY if policy_on else {}, method="PUT")
        for kind in ("drift", "valid"):
            for i in range(args.runs):
                adapter("context", RUN)
                intent = lax_intent()
                if kind == "drift":
                    xair_context({"line": {"state": "PAUSED"}})
                resp = adapter("intent", intent, mode="xair")
                rows.append({"policy": int(policy_on), "kind": kind, "run": i,
                             "gateway_released": int(released(resp)), "reason": resp.get("reason")})
    http_json(f"{XAIR}/v1/policy", {}, method="PUT")
    xair_context(RUN)
    write_csv(Path(args.out), rows)
    for p in (0, 1):
        for kind in ("drift", "valid"):
            cell = [r for r in rows if r["policy"] == p and r["kind"] == kind]
            k = sum(r["gateway_released"] for r in cell)
            print(json.dumps({"policy": p, "kind": kind, "released": f"{k}/{len(cell)}", "ci95": wilson_ci(k, len(cell))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
