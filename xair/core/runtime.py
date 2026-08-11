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
        self._metrics = {
            "intents_received": 0,
            "executed": 0,
            "revoked": 0,
            "delayed": 0,
            "degraded": 0,
            "validation_latencies_ms": [],
        }
        self._seen_ids: set[str] = set()

    def submit_intent(self, intent: ActionIntent) -> IntentRecord:
        if intent.id in self._seen_ids:
            record = self.lifecycle.get(intent.id)
            if record:
                return record
        self._seen_ids.add(intent.id)
        self.receiver.submit(intent)
        self._metrics["intents_received"] += 1
        return self.lifecycle.register(intent)

    def update_context(self, context: dict) -> None:
        self.context.update_context(context)

    def process_next(self, now: datetime | None = None) -> IntentRecord | None:
        intent = self.receiver.pop()
        if intent is None:
            return None
        return self.process_intent(intent, now=now)

    def process_intent(self, intent: ActionIntent, now: datetime | None = None) -> IntentRecord:
        now = now or datetime.now(timezone.utc)
        t0 = time.perf_counter()

        record = self.lifecycle.get(intent.id) or self.lifecycle.register(intent)
        self.lifecycle.transition(intent.id, IntentState.PENDING)
        self.lifecycle.transition(intent.id, IntentState.VALIDATING)

        conflict, conflict_reason = self.coordinator.check_conflict(intent)
        temporal_ok, temporal_reason = self.temporal.validate(intent, now)
        context_ok, context_reason = self.context.validate(intent)

        outcome, reason = self.decision_engine.decide(
            intent,
            temporal_ok,
            temporal_reason,
            context_ok,
            context_reason,
            resource_busy=conflict,
        )

        latency_ms = (time.perf_counter() - t0) * 1000.0
        self._metrics["validation_latencies_ms"].append(latency_ms)

        if outcome == DecisionOutcome.EXECUTE:
            self.coordinator.acquire(intent)
            self.lifecycle.transition(
                intent.id, IntentState.EXECUTED, outcome, reason, latency_ms
            )
            self._metrics["executed"] += 1
        elif outcome == DecisionOutcome.DEGRADE:
            transformed = self._apply_degradation(intent)
            self.lifecycle.transition(
                intent.id, IntentState.DEGRADED, outcome, reason, latency_ms
            )
            self._metrics["degraded"] += 1
            self.receiver.submit(transformed)
        elif outcome == DecisionOutcome.DELAY:
            self.lifecycle.transition(
                intent.id, IntentState.DELAYED, outcome, reason, latency_ms
            )
            self._metrics["delayed"] += 1
            self.receiver.submit(intent)
        else:
            state = IntentState.EXPIRED if "deadline" in reason else IntentState.REVOKED
            self.lifecycle.transition(intent.id, state, outcome, reason, latency_ms)
            self._metrics["revoked"] += 1

        if self.on_actuation and outcome == DecisionOutcome.EXECUTE:
            self.on_actuation(intent, outcome)

        if outcome in (DecisionOutcome.EXECUTE, DecisionOutcome.DEGRADE, DecisionOutcome.REVOKE):
            self.coordinator.release(intent)

        return self.lifecycle.get(intent.id)  # type: ignore[return-value]

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
        if policy == "reduced_speed":
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
