#!/usr/bin/env python3
"""E13: ingress faults, duplicate identifiers, and clock skew (pass criteria declared below).

  malformed_<kind>        5 structural fault templates (repeated); pass iff
                          nothing is released at the gateway.
  false_precondition      schema-valid intent whose precondition is false;
                          pass iff not released (reported apart from
                          malformed input: it is a semantic, not a
                          structural, fault).
  duplicate_id            same id submitted twice; pass iff the first is
                          released and the replay is *not* released again.
  clock_skew_past         decision 600 ms old, freshness 500 ms -> not released.
  clock_skew_future       decision 500 ms ahead (tolerance 1000 ms) -> released.
  clock_skew_far_future   decision 5 s ahead -> not released.
"""

from __future__ import annotations

import argparse
import json
import random
import urllib.request
import uuid
from pathlib import Path

from common import ADAPTER, RESULTS_DIR, adapter, now_iso, released, write_csv

RUN = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}


def post_raw(url: str, payload: bytes) -> tuple[int, dict]:
    r = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except Exception as e:
        return getattr(e, "code", 0) or 0, {"error": str(e)}


def intent_body(**kw) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": now_iso(),
        "freshness_window_ms": 500,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "payload": {"action_type": "RESUME", "target_entity": "line_1"},
    }
    base.update(kw)
    return base


MALFORMED = {
    "invalid_json": lambda: b"{not-json",
    "missing_fields": lambda: json.dumps({"id": "x"}).encode(),
    "trailing_garbage": lambda: json.dumps(intent_body()).encode() + b"\x00extra",
    "bad_timestamp": lambda: json.dumps({"timestamp_decision": "bad-ts", "freshness_window_ms": 500}).encode(),
    "wrong_types": lambda: json.dumps(intent_body(freshness_window_ms="soon", source=42)).encode(),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-per-fault", type=int, default=30)
    parser.add_argument("--malformed-count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default=str(RESULTS_DIR / "e13_faults.csv"))
    args = parser.parse_args()
    rng = random.Random(args.seed)
    rows = []

    adapter("context", RUN)
    kinds = sorted(MALFORMED)
    for i in range(args.malformed_count):
        kind = rng.choice(kinds)
        status, out = post_raw(f"{ADAPTER}/intent?mode=xair", MALFORMED[kind]())
        rows.append({"fault": f"malformed_{kind}", "run": i, "http_status": status,
                     "outcome": out.get("outcome"), "reason": out.get("reason") or out.get("error"),
                     "pass": not released(out)})

    for i in range(args.runs_per_fault):
        adapter("context", RUN)
        r = adapter("intent", intent_body(preconditions=[{"expr": "line.state == 'MAINTENANCE'"}]), mode="xair")
        rows.append({"fault": "false_precondition", "run": i, "outcome": r.get("outcome"), "reason": r.get("reason"),
                     "pass": not released(r)})

        intent = intent_body()
        r1 = adapter("intent", intent, mode="xair")
        r2 = adapter("intent", intent, mode="xair")
        rows.append({"fault": "duplicate_id", "run": i, "outcome": r2.get("outcome"), "reason": r2.get("reason"),
                     "first_released": int(released(r1)), "replay_released": int(released(r2)),
                     "pass": released(r1) and not released(r2) and bool(r2.get("duplicate"))})

        cases = (
            ("clock_skew_past", now_iso(-600), False),
            ("clock_skew_future", now_iso(+500), True),
            ("clock_skew_far_future", now_iso(+5000), False),
        )
        for fault, ts, expect_release in cases:
            r = adapter("intent", intent_body(timestamp_decision=ts), mode="xair")
            rows.append({"fault": fault, "run": i, "outcome": r.get("outcome"), "reason": r.get("reason"),
                         "pass": released(r) == expect_release})

    write_csv(Path(args.out), rows)
    by_fault: dict[str, list] = {}
    for r in rows:
        by_fault.setdefault(r["fault"], []).append(r)
    summary = {f: {"passed": sum(bool(x["pass"]) for x in xs), "total": len(xs)} for f, xs in sorted(by_fault.items())}
    print(json.dumps({"passed": sum(bool(r["pass"]) for r in rows), "total": len(rows), "by_fault": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
