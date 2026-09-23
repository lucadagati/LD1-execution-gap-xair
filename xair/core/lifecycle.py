from __future__ import annotations

from datetime import datetime, timezone

from xair.core.models import ActionIntent, DecisionOutcome, IntentRecord, IntentState

S = IntentState

# Admissible lifecycle transitions (paper Fig. 3). DELAYED and DEGRADED
# re-enter PENDING because the same identifier is requeued for a later
# validation pass; EXECUTED, REVOKED and EXPIRED are terminal.
ALLOWED_TRANSITIONS: dict[IntentState, frozenset[IntentState]] = {
    S.CREATED: frozenset({S.PENDING, S.REVOKED}),
    S.PENDING: frozenset({S.VALIDATING, S.REVOKED}),
    S.VALIDATING: frozenset({S.AUTHORIZED, S.DELAYED, S.DEGRADED, S.REVOKED, S.EXPIRED}),
    S.DELAYED: frozenset({S.PENDING, S.REVOKED, S.EXPIRED}),
    S.DEGRADED: frozenset({S.PENDING, S.REVOKED, S.EXPIRED}),
    S.AUTHORIZED: frozenset({S.EXECUTED, S.REVOKED}),
    S.EXECUTED: frozenset(),
    S.REVOKED: frozenset(),
    S.EXPIRED: frozenset(),
}
TERMINAL_STATES = frozenset(s for s, nxt in ALLOWED_TRANSITIONS.items() if not nxt)


class InvalidTransition(RuntimeError):
    """Raised when a caller requests a lifecycle transition the FSM forbids."""

    def __init__(self, intent_id: str, current: IntentState, requested: IntentState) -> None:
        super().__init__(f"{intent_id}: {current.value} -> {requested.value} not allowed")
        self.intent_id = intent_id
        self.current = current
        self.requested = requested


class LifecycleTracker:
    """FSM audit trail for action intents."""

    def __init__(self) -> None:
        self._records: dict[str, IntentRecord] = {}
        self._audit: list[dict] = []

    def register(self, intent: ActionIntent) -> IntentRecord:
        record = IntentRecord(intent=intent, state=IntentState.CREATED)
        self._records[intent.id] = record
        self._log(intent.id, IntentState.CREATED, "")
        return record

    def get(self, intent_id: str) -> IntentRecord | None:
        return self._records.get(intent_id)

    def forget(self, intent_id: str) -> None:
        self._records.pop(intent_id, None)

    def transition(
        self,
        intent_id: str,
        new_state: IntentState,
        outcome: DecisionOutcome | None = None,
        reason: str = "",
        latency_ms: float | None = None,
    ) -> IntentRecord:
        record = self._records[intent_id]
        if new_state not in ALLOWED_TRANSITIONS[record.state]:
            raise InvalidTransition(intent_id, record.state, new_state)
        record.state = new_state
        if outcome is not None:
            record.outcome = outcome
        record.reason = reason
        if latency_ms is not None:
            record.validation_latency_ms = latency_ms
        self._log(intent_id, new_state, reason, outcome)
        return record

    def _log(
        self,
        intent_id: str,
        state: IntentState,
        reason: str,
        outcome: DecisionOutcome | None = None,
    ) -> None:
        self._audit.append(
            {
                "intent_id": intent_id,
                "state": state.value,
                "outcome": outcome.value if outcome else None,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )

    @property
    def audit_log(self) -> list[dict]:
        return list(self._audit)

    def counts_by_outcome(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self._records.values():
            if r.outcome:
                counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
        return counts
