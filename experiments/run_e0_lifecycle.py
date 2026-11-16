#!/usr/bin/env python3
"""E0: lifecycle and contract regressions on the in-process runtime.

Deterministic. Authorization, commit, release and effect are distinct steps
(paper Fig. 2): the cases check that no intent counts as released because it
was authorized or committed, and that only an effect report confirms an effect.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from common import RESULTS_DIR

from xair.core.lifecycle import InvalidTransition
from xair.core.models import ActionIntent, DecisionOutcome, IntentState, ReleaseDecision
from xair.core.runtime import XAIRRuntime

RESULTS = RESULTS_DIR / "e0_lifecycle.json"


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


def _outcome(record) -> str | None:
    return record.outcome.value if record and record.outcome else None


def main() -> int:
    results = []
    running = {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}}

    rt = XAIRRuntime(context=running)
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict()))
    results.append({"case": "EXECUTE", "outcome": _outcome(o), "pass": o.outcome == DecisionOutcome.EXECUTE})

    rt = XAIRRuntime(context={"line": {"state": "PAUSED"}, "gripper": {"state": "OPEN"}})
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict()))
    results.append({"case": "REVOKE_precondition", "outcome": _outcome(o), "pass": o.outcome == DecisionOutcome.REVOKE})

    past = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    rt = XAIRRuntime(context=running)
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict(timestamp_decision=past, deadline_ms=100, freshness_window_ms=5000)))
    results.append({
        "case": "deadline_exceeded",
        "outcome": _outcome(o),
        "lifecycle_state": o.state.value,
        "pass": o.outcome == DecisionOutcome.REVOKE and o.state == IntentState.EXPIRED,
    })

    rt = XAIRRuntime(context=running)
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict(timestamp_decision=past, freshness_window_ms=50)))
    results.append({"case": "REVOKE_freshness", "outcome": _outcome(o), "pass": o.outcome == DecisionOutcome.REVOKE})

    rt = XAIRRuntime(context=running)
    holder = ActionIntent.from_dict(_intent_dict(id="hold-robot-3", payload={"action_type": "MOVE", "target_entity": "robot_3", "parameters": {}}))
    rt.coordinator.acquire(holder)
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict(payload={"action_type": "GRASP", "target_entity": "robot_3", "parameters": {}})))
    results.append({"case": "DELAY_busy_target", "outcome": _outcome(o), "pass": o.outcome == DecisionOutcome.DELAY})

    rt = XAIRRuntime(context=running)
    o = rt.process_intent(ActionIntent.from_dict(_intent_dict(payload={"action_type": "RESUME", "target_entity": "line_1", "parameters": {}, "degradation_policy": "reduce_speed"})))
    results.append({"case": "DEGRADE", "outcome": _outcome(o), "pass": o.outcome == DecisionOutcome.DEGRADE})

    iid = str(uuid.uuid4())
    rt = XAIRRuntime(context=running)
    intent = ActionIntent.from_dict(_intent_dict(
        id=iid,
        payload={"action_type": "RESUME", "target_entity": "line_1", "parameters": {}, "degradation_policy": "reduce_speed"},
    ))
    o_deg = rt.process_intent(intent)
    deg_outcome = _outcome(o_deg)
    o_exec = rt.process_next()
    exec_outcome = _outcome(o_exec)
    results.append({
        "case": "DEGRADE_then_EXECUTE_same_intent",
        "degrade_outcome": deg_outcome,
        "execute_outcome": exec_outcome,
        "same_intent_id": o_exec.intent.id == iid if o_exec else False,
        "speed_factor": o_exec.intent.payload.parameters.get("speed_factor") if o_exec else None,
        "pass": deg_outcome == "DEGRADE" and exec_outcome == "EXECUTE" and o_exec.intent.id == iid
                and o_exec.intent.payload.parameters.get("speed_factor") == 0.5,
    })

    # A revoked intent can never be published afterwards (FSM guard).
    rt = XAIRRuntime(context={"line": {"state": "PAUSED"}})
    revoked = ActionIntent.from_dict(_intent_dict())
    rt.process_intent(revoked)
    try:
        rt.report_release(revoked.id, True, "late_publish_attempt")
        refused = False
    except InvalidTransition:
        refused = True
    results.append({"case": "publish_after_revoke_refused", "pass": refused and rt.lifecycle.get(revoked.id).state == IntentState.REVOKED})

    # v' != v at t_g suppresses release even when the adapter reports publish.
    rt = XAIRRuntime(context=running)
    rec = rt.process_intent(ActionIntent.from_dict(_intent_dict()), context=running, context_version=7)
    final = rt.report_release(rec.intent.id, True, "gate_passed", context_version=8)
    results.append({
        "case": "version_mismatch_at_publish_blocks",
        "release_decision": final.release_decision.value,
        "pass": final.release_decision == ReleaseDecision.WITHHOLD and final.state == IntentState.WITHHELD,
    })

    # A duplicate id within the retention window is not re-validated or re-released.
    rt = XAIRRuntime(context=running)
    dup = ActionIntent.from_dict(_intent_dict())
    first, d1 = rt.submit_and_process(dup)
    rt.report_release(dup.id, True, "gate_passed")
    second, d2 = rt.submit_and_process(dup)
    m = rt.get_metrics()
    results.append({
        "case": "duplicate_not_rereleased",
        "pass": (not d1) and d2 and second is first and m["authorized"] == 1 and m["released"] == 1,
    })

    # An atomic commit is an authorization in the log, not a release or an effect.
    rt = XAIRRuntime(context=running)
    rec = rt.process_intent(ActionIntent.from_dict(_intent_dict()))
    rt.record_commit(rec.intent.id, True, "authorization_committed", seq=1)
    committed_state = rec.state
    try:
        rt.report_effect(rec.intent.id, "confirmed")
        effect_refused = False
    except InvalidTransition:
        effect_refused = True
    results.append({
        "case": "commit_is_not_release",
        "lifecycle_state": committed_state.value,
        "pass": committed_state == IntentState.COMMITTED and rec.release_decision is None and effect_refused,
    })

    # Release, then the controller's confirmation: COMMITTED -> RELEASED -> EFFECT_CONFIRMED.
    rt.report_release(rec.intent.id, True, "middleware_called")
    released_state = rec.state
    rt.report_effect(rec.intent.id, "confirmed", "executed:RUN:0")
    results.append({
        "case": "release_then_effect_confirmed",
        "states": [released_state.value, rec.state.value],
        "pass": released_state == IntentState.RELEASED and rec.state == IntentState.EFFECT_CONFIRMED,
    })

    # A version-conditional command the controller refused is a failed effect, not a success.
    rt = XAIRRuntime(context=running)
    rec = rt.process_intent(ActionIntent.from_dict(_intent_dict()))
    rt.report_release(rec.intent.id, True, "published_after_gate_recheck")
    rt.report_effect(rec.intent.id, "failed", "refused:version_changed:2")
    results.append({"case": "controller_refusal_is_failed_effect", "lifecycle_state": rec.state.value,
                    "pass": rec.state == IntentState.FAILED})

    # A refused commit withholds the intent; it can no longer be released.
    rt = XAIRRuntime(context=running)
    rec = rt.process_intent(ActionIntent.from_dict(_intent_dict()))
    rt.record_commit(rec.intent.id, False, "read_set_version_changed_at_commit")
    try:
        rt.report_release(rec.intent.id, True, "late_release")
        late_refused = False
    except InvalidTransition:
        late_refused = True
    results.append({"case": "refused_commit_withholds", "lifecycle_state": rec.state.value,
                    "pass": rec.state == IntentState.WITHHELD and late_refused})

    passed = sum(1 for r in results if r["pass"])
    out = {"passed": passed, "total": len(results), "cases": results}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
