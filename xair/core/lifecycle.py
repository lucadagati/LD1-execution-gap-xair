from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from xair.core.models import ActionIntent, DecisionOutcome, IntentRecord, IntentState


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

    def transition(
        self,
        intent_id: str,
        new_state: IntentState,
        outcome: DecisionOutcome | None = None,
        reason: str = "",
        latency_ms: float = 0.0,
    ) -> IntentRecord:
        record = self._records[intent_id]
        record.state = new_state
        if outcome is not None:
            record.outcome = outcome
        record.reason = reason
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
