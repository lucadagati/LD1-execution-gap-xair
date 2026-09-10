from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from xair.core.context_validator import ContextValidator
from xair.core.models import ActionIntent, DecisionOutcome, IntentState
from xair.core.runtime import XAIRRuntime


def intent_dict(**overrides) -> dict:
    body = {
        "id": str(uuid.uuid4()),
        "source": "ai",
        "timestamp_decision": datetime.now(timezone.utc).isoformat(),
        "freshness_window_ms": 1000,
        "deadline_ms": 1000,
        "preconditions": [{"expr": "line.state == 'RUN'"}],
        "safety_constraints": [],
        "payload": {
            "action_type": "RESUME",
            "target_entity": "line_1",
            "parameters": {},
            "degradation_policy": "none",
        },
    }
    body.update(overrides)
    return body


class ContractRuntimeTests(unittest.TestCase):
    def test_predicate_grammar_supports_single_equals_and_booleans(self) -> None:
        validator = ContextValidator({"line": {"state": "RUN"}, "robot": {"moving": True}})
        intent = ActionIntent.from_dict(
            intent_dict(preconditions=[{"expr": "line.state = 'RUN'"}, {"expr": "robot.moving == true"}])
        )
        self.assertEqual(validator.validate(intent), (True, "context_ok"))

    def test_empty_predicate_fails_closed(self) -> None:
        validator = ContextValidator({"line": {"state": "RUN"}})
        intent = ActionIntent.from_dict(intent_dict(preconditions=[{"expr": ""}]))
        ok, reason = validator.validate(intent)
        self.assertFalse(ok)
        self.assertIn("empty_expression", reason)

    def test_busy_valid_target_delays(self) -> None:
        runtime = XAIRRuntime(context={"line": {"state": "RUN"}})
        holder = ActionIntent.from_dict(intent_dict(payload={
            "action_type": "MOVE",
            "target_entity": "line_1",
            "parameters": {},
        }))
        runtime.coordinator.acquire(holder)
        record = runtime.process_intent(ActionIntent.from_dict(intent_dict()))
        self.assertEqual(record.outcome, DecisionOutcome.DELAY)
        self.assertEqual(record.state, IntentState.DELAYED)

    def test_busy_invalid_context_target_revokes_not_delays(self) -> None:
        # Reference Model (Sec. V): DELAY is reserved for an otherwise
        # semantically valid intent. A context-invalid intent on a busy
        # target must REVOKE, not silently DELAY behind the busy target.
        runtime = XAIRRuntime(context={"line": {"state": "PAUSED"}})
        holder = ActionIntent.from_dict(intent_dict(payload={
            "action_type": "MOVE",
            "target_entity": "line_1",
            "parameters": {},
        }))
        runtime.coordinator.acquire(holder)
        record = runtime.process_intent(ActionIntent.from_dict(intent_dict()))
        self.assertEqual(record.outcome, DecisionOutcome.REVOKE)
        self.assertEqual(record.state, IntentState.REVOKED)

    def test_degradation_is_applied_then_revalidated(self) -> None:
        # Reference Model (Fig. 2 FSM): DEGRADE returns the intent to VALIDATING
        # rather than resolving to EXECUTE within the same process_intent() call.
        # A first pass yields DEGRADE with the transformed payload requeued; a
        # second, independent pass (process_next()) then authorizes it.
        runtime = XAIRRuntime(context={"line": {"state": "RUN"}})
        body = intent_dict()
        body["payload"]["degradation_policy"] = "reduce_speed"
        intent = ActionIntent.from_dict(body)
        degraded = runtime.process_intent(intent)
        self.assertEqual(degraded.outcome, DecisionOutcome.DEGRADE)
        self.assertEqual(degraded.state, IntentState.DEGRADED)
        self.assertEqual(degraded.intent.payload.parameters["speed_factor"], 0.5)

        executed = runtime.process_next()
        self.assertIsNotNone(executed)
        self.assertEqual(executed.intent.id, intent.id)
        self.assertEqual(executed.outcome, DecisionOutcome.EXECUTE)
        self.assertEqual(executed.state, IntentState.AUTHORIZED)
        self.assertEqual(executed.intent.payload.parameters["speed_factor"], 0.5)

        states = [event["state"] for event in runtime.lifecycle.audit_log]
        self.assertIn("DEGRADED", states)

    def test_publication_is_a_separate_idempotent_transition(self) -> None:
        actuations: list[str] = []
        runtime = XAIRRuntime(
            context={"line": {"state": "RUN"}},
            on_actuation=lambda intent, _outcome: actuations.append(intent.id),
        )
        intent = ActionIntent.from_dict(intent_dict())
        record, duplicate = runtime.submit_and_process(intent)
        self.assertFalse(duplicate)
        self.assertEqual(record.state, IntentState.AUTHORIZED)

        final = runtime.confirm_publication(intent.id, True, "gate_passed", context_version=4)
        replay = runtime.confirm_publication(intent.id, True, "gate_passed", context_version=4)
        self.assertEqual(final.state, IntentState.EXECUTED)
        self.assertEqual(replay.publication_decision, "PUBLISH")
        self.assertEqual(actuations, [intent.id])

    def test_duplicate_intent_is_not_reauthorized(self) -> None:
        runtime = XAIRRuntime(context={"line": {"state": "RUN"}})
        intent = ActionIntent.from_dict(intent_dict())
        first, duplicate_first = runtime.submit_and_process(intent)
        second, duplicate_second = runtime.submit_and_process(intent)
        self.assertFalse(duplicate_first)
        self.assertTrue(duplicate_second)
        self.assertIs(first, second)
        self.assertEqual(runtime.get_metrics()["intents_received"], 1)

    def test_older_snapshot_cannot_roll_back_validation_context(self) -> None:
        runtime = XAIRRuntime()
        runtime.install_context_snapshot({"line": {"state": "PAUSED"}}, 2)
        effective = runtime.install_context_snapshot({"line": {"state": "RUN"}}, 1)
        self.assertEqual(effective, 2)
        record, duplicate = runtime.submit_and_process(
            ActionIntent.from_dict(intent_dict()),
            context_snapshot={"line": {"state": "RUN"}},
            context_version=1,
        )
        self.assertFalse(duplicate)
        self.assertEqual(record.context_version, 2)
        self.assertEqual(record.outcome, DecisionOutcome.REVOKE)

    def test_schema_uses_the_runtime_field_names(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "action-intent-v1.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        properties = schema["properties"]
        self.assertIn("deadline_ms", properties)
        self.assertIn("safety_constraints", properties)
        self.assertIn("mes", properties["source"]["enum"])
        self.assertIn("reduce_speed", schema["$defs"]["action_descriptor"]["properties"]["degradation_policy"]["enum"])


if __name__ == "__main__":
    unittest.main()