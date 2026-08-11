#!/usr/bin/env python3
"""E0: lifecycle FSM smoke tests — documents actual runtime outcomes."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xair.core.models import ActionIntent, DecisionOutcome, IntentState
from xair.core.runtime import XAIRRuntime

RESULTS = ROOT / "experiments" / "results" / "e0_lifecycle.json"


def _intent_dict(**kw) -> dict:
    defaults = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "freshness_window_ms": 500,
        "deadline_ms": 800,
        "preconditions": ["line.state == 'RUN'"],
        "payload": {"action_type": "RESUME", "target_entity": "line_1", "parameters": {}},
    }
    defaults.update(kw)
    return defaults


def main() -> int:
    results = []

    rt = XAIRRuntime(context={"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict()))
    results.append({"case": "EXECUTE", "outcome": o.outcome.value if o.outcome else None, "pass": o.outcome == DecisionOutcome.EXECUTE})

    rt = XAIRRuntime(context={"line": {"state": "PAUSED"}, "gripper": {"state": "OPEN"}})
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict()))
    results.append({"case": "REVOKE_precondition", "outcome": o.outcome.value if o.outcome else None, "pass": o.outcome == DecisionOutcome.REVOKE})

    past = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    rt = XAIRRuntime(context={"line": {"state": "RUN"}})
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict(timestamp_decision=past, deadline_ms=100, freshness_window_ms=5000)))
    results.append({
        "case": "deadline_exceeded",
        "outcome": o.outcome.value if o.outcome else None,
        "lifecycle_state": o.state.value,
        "pass": o.outcome == DecisionOutcome.REVOKE and o.state == IntentState.EXPIRED,
    })

    rt = XAIRRuntime(context={"line": {"state": "RUN"}})
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict(timestamp_decision=past, freshness_window_ms=50)))
    results.append({"case": "REVOKE_freshness", "outcome": o.outcome.value if o.outcome else None, "pass": o.outcome == DecisionOutcome.REVOKE})

    rt = XAIRRuntime(context={"line": {"state": "PAUSED"}, "gripper": {"state": "OPEN"}})
    holder = ActionIntent.from_dict(_intent_dict(id="hold-robot-3", payload={"action_type": "MOVE", "target_entity": "robot_3", "parameters": {}}))
    rt.coordinator.acquire(holder)
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict(payload={"action_type": "GRASP", "target_entity": "robot_3", "parameters": {}})))
    results.append({"case": "DELAY_busy_target", "outcome": o.outcome.value if o.outcome else None, "pass": o.outcome == DecisionOutcome.DELAY})

    rt = XAIRRuntime(context={"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict(payload={"action_type": "RESUME", "target_entity": "line_1", "parameters": {}, "degradation_policy": "reduced_speed"})))
    results.append({"case": "DEGRADE", "outcome": o.outcome.value if o.outcome else None, "pass": o.outcome == DecisionOutcome.DEGRADE})

    iid = str(uuid.uuid4())
    rt3 = XAIRRuntime(context={"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}})
    intent = ActionIntent.from_dict(_intent_dict(
        id=iid,
        payload={"action_type": "RESUME", "target_entity": "line_1", "parameters": {}, "degradation_policy": "reduced_speed"},
    ))
    o_deg = rt3.process_intent(intent)
    deg_outcome = o_deg.outcome.value if o_deg.outcome else None
    o_exec = rt3.process_next()
    exec_outcome = o_exec.outcome.value if o_exec and o_exec.outcome else None
    results.append({
        "case": "DEGRADE_then_EXECUTE_same_intent",
        "degrade_outcome": deg_outcome,
        "execute_outcome": exec_outcome,
        "same_intent_id": o_exec.intent.id == iid if o_exec else False,
        "speed_factor": o_exec.intent.payload.parameters.get("speed_factor") if o_exec else None,
        "pass": (
            deg_outcome == "DEGRADE"
            and o_exec is not None
            and exec_outcome == "EXECUTE"
            and o_exec.intent.id == iid
        ),
    })

    passed = sum(1 for r in results if r["pass"])
    out = {"passed": passed, "total": len(results), "cases": results}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
