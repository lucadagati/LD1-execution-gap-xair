from __future__ import annotations

from datetime import datetime, timezone

from xair.core.models import ActionIntent, DecisionOutcome, IntentRecord, IntentState

S = IntentState

# Admissible lifecycle transitions (paper Fig. 3). DELAYED and DEGRADED
# re-enter PENDING because the same identifier is requeued for a later
# validation pass. After AUTHORIZED, the optimistic path goes to RELEASED or
# WITHHELD on the gateway's report; the atomic path goes to COMMITTED (or
# WITHHELD) on the commit, then to RELEASED (or WITHHELD, by a release guard)
# on the gateway's report. Only an effect report moves a released intent to
# EFFECT_CONFIRMED or FAILED; without one it is UNKNOWN_EFFECT, which a late
# report can still resolve. A supervisory revoke is possible until the
# authorization is committed or released, not after.
ALLOWED_TRANSITIONS: dict[IntentState, frozenset[IntentState]] = {
    S.CREATED: frozenset({S.PENDING, S.REVOKED}),
    S.PENDING: frozenset({S.VALIDATING, S.REVOKED}),
    S.VALIDATING: frozenset({S.AUTHORIZED, S.DELAYED, S.DEGRADED, S.REVOKED, S.EXPIRED}),
    S.DELAYED: frozenset({S.PENDING, S.REVOKED, S.EXPIRED}),
    S.DEGRADED: frozenset({S.PENDING, S.REVOKED, S.EXPIRED}),
    S.AUTHORIZED: frozenset({S.COMMITTED, S.RELEASED, S.WITHHELD, S.REVOKED}),
    S.COMMITTED: frozenset({S.RELEASED, S.WITHHELD}),
    S.RELEASED: frozenset({S.EFFECT_CONFIRMED, S.FAILED, S.UNKNOWN_EFFECT}),
    S.UNKNOWN_EFFECT: frozenset({S.EFFECT_CONFIRMED, S.FAILED}),
    S.WITHHELD: frozenset(),
    S.EFFECT_CONFIRMED: frozenset(),
    S.FAILED: frozenset(),
    S.REVOKED: frozenset(),
    S.EXPIRED: frozenset(),
}
TERMINAL_STATES = frozenset(s for s, nxt in ALLOWED_TRANSITIONS.items() if not nxt)
# States whose record may be forgotten once the idempotency window has passed:
# terminal ones, and UNKNOWN_EFFECT, whose resolution is optional.
SETTLED_STATES = TERMINAL_STATES | {S.UNKNOWN_EFFECT}
# Released into middleware or a controller (the release indicator Y of the paper).
RELEASED_STATES = frozenset({S.RELEASED, S.EFFECT_CONFIRMED, S.FAILED, S.UNKNOWN_EFFECT})


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

    @staticmethod
    def allowed(state: IntentState) -> frozenset[IntentState]:
        return ALLOWED_TRANSITIONS[state]

    def note(self, intent_id: str, reason: str) -> None:
        """Append an audit entry without changing state (e.g. a rejected report)."""
        record = self._records.get(intent_id)
        if record is not None:
            self._log(intent_id, record.state, reason)

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
