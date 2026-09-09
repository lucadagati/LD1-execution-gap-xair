from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from xair.core.context_validator import ContextValidator
from xair.core.coordinator import DistributedCoordinator
from xair.core.execution_decision import ExecutionDecisionEngine
from xair.core.intent_receiver import IntentReceiver
from xair.core.lifecycle import LifecycleTracker
from xair.core.models import ActionIntent, DecisionOutcome, IntentState, IntentRecord
from xair.core.temporal_validator import TemporalValidator


ActuationCallback = Callable[[ActionIntent, DecisionOutcome], None]


class XAIRRuntime:
    """Orchestrates intent reception, validation, decision, and lifecycle."""

    def __init__(
        self,
        context: dict | None = None,
        on_actuation: ActuationCallback | None = None,
    ) -> None:
        self.receiver = IntentReceiver()
        self.temporal = TemporalValidator()
        self.context = ContextValidator(context)
        self.decision_engine = ExecutionDecisionEngine()
        self.coordinator = DistributedCoordinator()
        self.lifecycle = LifecycleTracker()
        self.on_actuation = on_actuation
        self._context_version = 0
        self._metrics = {
            "intents_received": 0,
            "executed": 0,
            "revoked": 0,
            "delayed": 0,
            "degraded": 0,
            "validation_latencies_ms": [],
        }
        self._seen_ids: set[str] = set()

    def install_context_snapshot(self, snapshot: dict, version: int) -> int:
        if version < self._context_version:
            return self._context_version
        self._context_version = max(self._context_version, version)
        self.context.update_context(snapshot)
        return self._context_version

    def submit_intent(self, intent: ActionIntent) -> IntentRecord:
        if intent.id in self._seen_ids:
            record = self.lifecycle.get(intent.id)
            if record:
                return record
        self._seen_ids.add(intent.id)
        self.receiver.submit(intent)
        self._metrics["intents_received"] += 1
        return self.lifecycle.register(intent)

    def submit_and_process(
        self,
        intent: ActionIntent,
        *,
        context_snapshot: dict | None = None,
        context_version: int | None = None,
        now: datetime | None = None,
    ) -> tuple[IntentRecord, bool]:
        duplicate = intent.id in self._seen_ids
        if context_snapshot is not None and context_version is not None:
            self.install_context_snapshot(context_snapshot, context_version)
        if duplicate:
            return self.lifecycle.get(intent.id), True  # type: ignore[return-value]
        self.submit_intent(intent)
        record = self.process_intent(intent, now=now, context_version=context_version)
        return record, False

    def update_context(self, context: dict) -> None:
        self.context.update_context(context)

    def process_next(self, now: datetime | None = None) -> IntentRecord | None:
        intent = self.receiver.pop()
        if intent is None:
            return None
        return self.process_intent(intent, now=now)

    def process_intent(
        self,
        intent: ActionIntent,
        now: datetime | None = None,
        *,
        context_version: int | None = None,
    ) -> IntentRecord:
        now = now or datetime.now(timezone.utc)
        t0 = time.perf_counter()
        effective_version = self._context_version
        if context_version is not None and context_version < effective_version:
            record = self.lifecycle.get(intent.id) or self.lifecycle.register(intent)
            self.lifecycle.transition(intent.id, IntentState.PENDING)
            self.lifecycle.transition(intent.id, IntentState.VALIDATING)
            record.context_version = effective_version
            self.lifecycle.transition(
                intent.id,
                IntentState.REVOKED,
                DecisionOutcome.REVOKE,
                "stale_context_snapshot",
            )
            self._metrics["revoked"] += 1
            return self.lifecycle.get(intent.id)  # type: ignore[return-value]

        working = intent
        record = self.lifecycle.get(intent.id) or self.lifecycle.register(intent)
        self.lifecycle.transition(intent.id, IntentState.PENDING)
        self.lifecycle.transition(intent.id, IntentState.VALIDATING)

        conflict, _conflict_reason = self.coordinator.check_conflict(working)
        temporal_ok, temporal_reason = self.temporal.validate(working, now)
        context_ok, context_reason = self.context.validate(working)
        outcome, reason = self.decision_engine.decide(
            working,
            temporal_ok,
            temporal_reason,
            context_ok,
            context_reason,
            resource_busy=conflict,
        )

        latency_ms = (time.perf_counter() - t0) * 1000.0
        self._metrics["validation_latencies_ms"].append(latency_ms)
        record = self.lifecycle.get(intent.id)  # type: ignore[assignment]
        record.context_version = effective_version
        record.validation_latency_ms = latency_ms

        if outcome == DecisionOutcome.DEGRADE:
            # Transform the payload, clear the degradation policy, and return the
            # *same* identifier to the pending queue for a fresh validation pass
            # (Reference Model: DEGRADE re-enters VALIDATING rather than resolving
            # to EXECUTE within one call).
            working = self._apply_degradation(working)
            record.intent = working
            self.lifecycle.transition(
                working.id, IntentState.DEGRADED, outcome, reason, latency_ms
            )
            self._metrics["degraded"] += 1
            self.receiver.submit(working)
            return self.lifecycle.get(intent.id)  # type: ignore[return-value]

        if outcome == DecisionOutcome.EXECUTE:
            self.coordinator.acquire(working)
            record.intent = working
            self.lifecycle.transition(
                working.id, IntentState.AUTHORIZED, DecisionOutcome.EXECUTE, reason, latency_ms
            )
            self._metrics["executed"] += 1
        elif outcome == DecisionOutcome.DELAY:
            self.lifecycle.transition(
                working.id, IntentState.DELAYED, outcome, reason, latency_ms
            )
            self._metrics["delayed"] += 1
            self.receiver.submit(working)
        else:
            state = IntentState.EXPIRED if "deadline" in reason else IntentState.REVOKED
            self.lifecycle.transition(working.id, state, outcome, reason, latency_ms)
            self._metrics["revoked"] += 1

        return self.lifecycle.get(intent.id)  # type: ignore[return-value]

    def confirm_publication(
        self,
        intent_id: str,
        publish: bool,
        reason: str,
        *,
        context_version: int | None = None,
    ) -> IntentRecord:
        record = self.lifecycle.get(intent_id)
        if record is None:
            raise KeyError(intent_id)
        if record.publication_decision == "PUBLISH":
            return record
        if not publish:
            self.coordinator.release(record.intent)
            self.lifecycle.transition(
                intent_id, IntentState.REVOKED, DecisionOutcome.REVOKE, reason
            )
            record.publication_decision = "BLOCK"
            return record
        if context_version is not None and context_version < record.context_version:
            self.coordinator.release(record.intent)
            self.lifecycle.transition(
                intent_id, IntentState.REVOKED, DecisionOutcome.REVOKE, "context_version_changed_at_publish"
            )
            record.publication_decision = "BLOCK"
            return record
        record.publication_decision = "PUBLISH"
        self.lifecycle.transition(intent_id, IntentState.EXECUTED, DecisionOutcome.EXECUTE, reason)
        if self.on_actuation:
            self.on_actuation(record.intent, DecisionOutcome.EXECUTE)
        self.coordinator.release(record.intent)
        return record

    def process_all(self, now: datetime | None = None) -> list[IntentRecord]:
        results = []
        while True:
            r = self.process_next(now=now)
            if r is None:
                break
            results.append(r)
        return results

    def _apply_degradation(self, intent: ActionIntent) -> ActionIntent:
        """Transform payload and clear degradation policy for same-intent revalidation."""
        policy = intent.payload.degradation_policy
        params = dict(intent.payload.parameters)
        if policy == "reduce_speed":
            params["speed_factor"] = params.get("speed_factor", 1.0) * 0.5
        intent.payload.parameters = params
        intent.payload.degradation_policy = "none"
        return intent

    def get_metrics(self) -> dict:
        lat = self._metrics["validation_latencies_ms"]
        p99 = sorted(lat)[int(len(lat) * 0.99)] if lat else 0.0
        p50 = sorted(lat)[len(lat) // 2] if lat else 0.0
        total_exec = self._metrics["executed"] + self._metrics["degraded"]
        stale = self._metrics["revoked"]
        ser = stale / max(total_exec + stale, 1) if stale else 0.0
        return {
            **self._metrics,
            "validation_latency_p50_ms": p50,
            "validation_latency_p99_ms": p99,
            "ser_proxy": ser,
        }
