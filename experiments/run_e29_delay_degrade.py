#!/usr/bin/env python3
"""E29: the two lifecycle transitions that the other experiments do not exercise, DELAY and DEGRADE, measured.

DELAY: while an intent holds a target, a second intent on it must be delayed, must not be released while the
holder is, and must be executed once the holder is released (resubmission of the delayed one).
DEGRADE: an intent with the reduce_speed policy must be degraded, and its second pass, under the same id, must
execute with the transformed payload (speed factor 0.5) and no other change.
Each trial records the outcome sequence and the validation latency of every pass (in-process runtime, no network).
"""
from __future__ import annotations

import argparse
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common import write_csv
from xair.core.models import ActionIntent, DecisionOutcome
from xair.core.runtime import XAIRRuntime


def intent(**kw):
    d = {"id": str(uuid.uuid4()), "source": "ai",
         "timestamp_decision": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
         "freshness_window_ms": 500, "deadline_ms": 800, "preconditions": ["line.state == 'RUN'"],
         "payload": {"action_type": "RESUME", "target_entity": "line_1", "parameters": {}}}
    d.update(kw)
    return ActionIntent.from_dict(d)


def delay_trial(i: int) -> dict:
    rt = XAIRRuntime(context={"line": {"state": "RUN"}})
    target = f"robot_{i}"
    holder = intent(payload={"action_type": "MOVE", "target_entity": target, "parameters": {}})
    rt.coordinator.acquire(holder)
    second = intent(payload={"action_type": "GRASP", "target_entity": target, "parameters": {}})
    t0 = time.perf_counter()
    first = rt.process_intent(second).outcome
    t1 = time.perf_counter()
    rt.coordinator.release(holder)
    again = rt.process_intent(second).outcome   # the record is live: read the outcome at once
    t2 = time.perf_counter()
    ok = first == DecisionOutcome.DELAY and again == DecisionOutcome.EXECUTE
    return {"case": "delay", "run": i, "first": first.value, "second": again.value,
            "first_ms": (t1 - t0) * 1e3, "second_ms": (t2 - t1) * 1e3, "correct": int(ok)}


def degrade_trial(i: int) -> dict:
    rt = XAIRRuntime(context={"line": {"state": "RUN"}})
    it = intent(payload={"action_type": "RESUME", "target_entity": f"line_{i}", "parameters": {}, "degradation_policy": "reduce_speed"})
    t0 = time.perf_counter()
    first = rt.process_intent(it).outcome
    t1 = time.perf_counter()
    second = rt.process_next()
    t2 = time.perf_counter()
    p = second.intent.payload.parameters
    ok = (first == DecisionOutcome.DEGRADE and second.outcome == DecisionOutcome.EXECUTE
          and second.intent.id == it.id and p.get("speed_factor") == 0.5)
    return {"case": "degrade", "run": i, "first": first.value, "second": second.outcome.value,
            "first_ms": (t1 - t0) * 1e3, "second_ms": (t2 - t1) * 1e3, "correct": int(ok)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=300)
    ap.add_argument("--out", default=str(Path(__file__).parent / "results" / "e29_delay_degrade.csv"))
    a = ap.parse_args()
    rows = [f(i) for i in range(a.runs) for f in (delay_trial, degrade_trial)]
    write_csv(Path(a.out), rows)
    for c in ("delay", "degrade"):
        r = [x for x in rows if x["case"] == c]
        print(c, f"{sum(x['correct'] for x in r)}/{len(r)}", f"median passes {statistics.median(x['first_ms'] for x in r):.3f}/{statistics.median(x['second_ms'] for x in r):.3f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
