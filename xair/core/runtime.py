from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable

from xair.core.context_validator import ContextValidator
from xair.core.coordinator import DistributedCoordinator
from xair.core.execution_decision import ExecutionDecisionEngine
from xair.core.intent_receiver import IntentReceiver
from xair.core.lifecycle import TERMINAL_STATES, InvalidTransition, LifecycleTracker
from xair.core.models import ActionIntent, DecisionOutcome, IntentRecord, IntentState
from xair.core.temporal_validator import TemporalValidator
from xair.core.versioning import read_set, read_set_version

ActuationCallback = Callable[[ActionIntent, DecisionOutcome], None]

DEFAULT_IDEMPOTENCY_RETENTION_S = 3600.0
_VALIDATABLE_STATES = frozenset({IntentState.CREATED, IntentState.DELAYED, IntentState.DEGRADED})


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (the definition used for every reported figure)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered) - 1e-9))
    return ordered[min(rank, len(ordered)) - 1]


class XAIRRuntime:
    """Orchestrates intent reception, validation, decision, and lifecycle.

    All mutations of shared state (lifecycle records, idempotency table,
    resource locks, queue) happen under one re-entrant lock, so concurrent
    HTTP workers observe a serializable sequence of admissions and decisions.
    Predicate evaluation itself is stateless and runs on the snapshot the
    caller passes in.
    """

    def __init__(
        self,
        context: dict | None = None,
        on_actuation: ActuationCallback | None = None,
        *,
        idempotency_retention_s: float = DEFAULT_IDEMPOTENCY_RETENTION_S,
    ) -> None:
        self.receiver = IntentReceiver()
        self.temporal = TemporalValidator()
        self.context = ContextValidator(context)
        self.decision_engine = ExecutionDecisionEngine()
        self.coordinator = DistributedCoordinator()
        self.lifecycle = LifecycleTracker()
        self.on_actuation = on_actuation
        self.idempotency_retention_s = idempotency_retention_s
        self._lock = threading.RLock()
        self._context_version = 0
        self._metrics = {
            "intents_received": 0,
            "executed": 0,
            "revoked": 0,
            "delayed": 0,
            "degraded": 0,
            "validation_latencies_ms": [],
        }
        # intent id -> monotonic admission time, oldest first.
        self._seen: OrderedDict[str, float] = OrderedDict()

    # ------------------------------------------------------------------ context

    def install_context_snapshot(self, snapshot: dict, version: int, *, replace: bool = False) -> int:
        """Install a snapshot unless it is older than the one already installed."""
        with self._lock:
            if version < self._context_version:
                return self._context_version
            self._context_version = version
            if replace:
                self.context.replace_context(snapshot)
            else:
                self.context.update_context(snapshot)
            return self._context_version

    def update_context(self, context: dict) -> None:
        with self._lock:
            self.context.update_context(context)

    # -------------------------------------------------------------- admission

    def _expire_seen(self) -> None:
        horizon = time.monotonic() - self.idempotency_retention_s
        while self._seen:
            intent_id, admitted_at = next(iter(self._seen.items()))
            if admitted_at >= horizon:
                break
            self._seen.popitem(last=False)
            record = self.lifecycle.get(intent_id)
            if record is not None and record.state in TERMINAL_STATES:
                self.lifecycle.forget(intent_id)

    def is_duplicate(self, intent_id: str) -> bool:
        with self._lock:
            self._expire_seen()
            return intent_id in self._seen

    def admit(self, intent: ActionIntent, *, enqueue: bool = False) -> tuple[IntentRecord, bool]:
        """Register an intent once per retention window; return (record, duplicate)."""
        with self._lock:
            self._expire_seen()
            if intent.id in self._seen:
                record = self.lifecycle.get(intent.id)
                if record is not None:
                    return record, True
            self._seen[intent.id] = time.monotonic()
            self._metrics["intents_received"] += 1
            record = self.lifecycle.register(intent)
            if enqueue:
                self.receiver.submit(intent)
            return record, False

    def submit_intent(self, intent: ActionIntent) -> IntentRecord:
        """Admit and enqueue for a later process_next() pass."""
        record, _ = self.admit(intent, enqueue=True)
        return record

    def submit_and_process(
        self,
        intent: ActionIntent,
        *,
        context_snapshot: dict | None = None,
        context_version: int | None = None,
        now: datetime | None = None,
    ) -> tuple[IntentRecord, bool]:
        with self._lock:
            if context_snapshot is not None and context_version is not None:
                self.install_context_snapshot(context_snapshot, context_version)
            record, duplicate = self.admit(intent)
            if duplicate:
                return record, True
            return self.process_intent(intent, now=now, context_version=context_version), False

    # ------------------------------------------------------------- validation

    def process_next(self, now: datetime | None = None) -> IntentRecord | None:
        with self._lock:
            intent = self.receiver.pop()
            if intent is None:
                return None
            return self.process_intent(intent, now=now)

    def process_intent(
        self,
        intent: ActionIntent,
        now: datetime | None = None,
        *,
        context: dict | None = None,
        context_version: int | None = None,
        path_versions: dict[str, int] | None = None,
    ) -> IntentRecord:
        """Validate one intent at t_v.

        ``context``/``context_version`` are the snapshot the caller read
        atomically from the store; when omitted, the installed snapshot is
        used. A snapshot older than the installed one is rejected as stale.
        """
        with self._lock:
            record = self.lifecycle.get(intent.id) or self.lifecycle.register(intent)
            if record.state not in _VALIDATABLE_STATES:
                # Terminal, or already authorized and awaiting its t_p report:
                # a second validation pass must not re-acquire the target.
                return record
            now = now or datetime.now(timezone.utc)
            t0 = time.perf_counter()
            installed = self._context_version
            if context is None:
                snapshot, version = self.context.context, installed
            else:
                snapshot, version = context, (installed if context_version is None else context_version)

            self.lifecycle.transition(intent.id, IntentState.PENDING)
            self.lifecycle.transition(intent.id, IntentState.VALIDATING)
            if context_version is not None and context_version < installed:
                record.context_version = installed
                self.lifecycle.transition(
                    intent.id, IntentState.REVOKED, DecisionOutcome.REVOKE, "stale_context_snapshot"
                )
                self._metrics["revoked"] += 1
                return record

            conflict, _ = self.coordinator.check_conflict(intent)
            temporal_ok, temporal_reason = self.temporal.validate(intent, now)
            context_ok, context_reason = self.context.validate(intent, snapshot)
            outcome, reason = self.decision_engine.decide(
                intent, temporal_ok, temporal_reason, context_ok, context_reason, resource_busy=conflict
            )

            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._metrics["validation_latencies_ms"].append(latency_ms)
            record.context_version = version
            record.read_set = read_set([*intent.safety_constraints, *intent.preconditions])
            record.read_set_version = (
                read_set_version(path_versions, record.read_set) if path_versions is not None else version
            )
            record.validation_latency_ms = latency_ms

            if outcome == DecisionOutcome.DEGRADE:
                # Transform the payload, clear the degradation policy, and return
                # the *same* identifier to the pending queue for a fresh pass.
                record.intent = self._apply_degradation(intent)
                self.lifecycle.transition(intent.id, IntentState.DEGRADED, outcome, reason, latency_ms)
                self._metrics["degraded"] += 1
                self.receiver.submit(record.intent)
            elif outcome == DecisionOutcome.EXECUTE:
                self.coordinator.acquire(intent)
                record.intent = intent
                self.lifecycle.transition(intent.id, IntentState.AUTHORIZED, outcome, reason, latency_ms)
                self._metrics["executed"] += 1
            elif outcome == DecisionOutcome.DELAY:
                self.lifecycle.transition(intent.id, IntentState.DELAYED, outcome, reason, latency_ms)
                self._metrics["delayed"] += 1
                self.receiver.submit(intent)
            else:
                state = IntentState.EXPIRED if reason.startswith("deadline") else IntentState.REVOKED
                self.lifecycle.transition(intent.id, state, outcome, reason, latency_ms)
                self._metrics["revoked"] += 1
            return record

    def process_all(self, now: datetime | None = None) -> list[IntentRecord]:
        results = []
        while (r := self.process_next(now=now)) is not None:
            results.append(r)
        return results

    # ------------------------------------------------------------ publication

    def confirm_publication(
        self,
        intent_id: str,
        publish: bool,
        reason: str,
        *,
        context_version: int | None = None,
        read_set_version: int | None = None,
    ) -> IntentRecord:
        """Close the gate (t_g) reported by the actuator gateway.

        Only an AUTHORIZED intent can be published. The gateway reports the
        version it observed at t_g: the read-set version (default scope) or
        the global version. A report that differs from the value recorded at
        t_v is converted into a suppression. Replays of an already published
        intent are idempotent.
        """
        with self._lock:
            record = self.lifecycle.get(intent_id)
            if record is None:
                raise KeyError(intent_id)
            if record.publication_decision == "PUBLISH":
                return record
            if record.state != IntentState.AUTHORIZED:
                raise InvalidTransition(
                    intent_id, record.state, IntentState.EXECUTED if publish else IntentState.REVOKED
                )
            if publish and read_set_version is not None and read_set_version != record.read_set_version:
                publish, reason = False, "read_set_version_changed_at_gate"
            elif publish and read_set_version is None and context_version is not None and context_version != record.context_version:
                publish, reason = False, "context_version_changed_at_publish"
            self.coordinator.release(record.intent)
            if not publish:
                self.lifecycle.transition(intent_id, IntentState.REVOKED, DecisionOutcome.REVOKE, reason)
                record.publication_decision = "BLOCK"
                return record
            record.publication_decision = "PUBLISH"
            self.lifecycle.transition(intent_id, IntentState.EXECUTED, DecisionOutcome.EXECUTE, reason)
            if self.on_actuation:
                self.on_actuation(record.intent, DecisionOutcome.EXECUTE)
            return record

    def supervisory_revoke(self, intent_id: str, reason: str = "human_supervisory_revoke") -> IntentRecord:
        """Revoke a non-terminal intent; an executed intent cannot be rolled back."""
        with self._lock:
            record = self.lifecycle.get(intent_id)
            if record is None:
                raise KeyError(intent_id)
            self.lifecycle.transition(intent_id, IntentState.REVOKED, DecisionOutcome.REVOKE, reason)
            self.coordinator.release(record.intent)
            return record

    # ---------------------------------------------------------------- helpers

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
        with self._lock:
            lat = list(self._metrics["validation_latencies_ms"])
            decided = self._metrics["executed"] + self._metrics["degraded"] + self._metrics["revoked"]
            return {
                **{k: v for k, v in self._metrics.items() if k != "validation_latencies_ms"},
                "validation_latency_p50_ms": percentile(lat, 0.50),
                "validation_latency_p99_ms": percentile(lat, 0.99),
                # Share of decided intents that were revoked at t_v. This is an
                # operational counter, not SER: SER needs ground-truth labels.
                "revoke_fraction": self._metrics["revoked"] / decided if decided else 0.0,
            }
