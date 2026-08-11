from __future__ import annotations

from datetime import datetime, timezone

from xair.core.models import ActionIntent


class TemporalValidator:
    """Verify freshness window, deadline, and bounded future clock skew."""

    def __init__(self, max_future_skew_ms: float = 1000.0) -> None:
        self.max_future_skew_ms = max_future_skew_ms

    def validate(self, intent: ActionIntent, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        decision = intent.timestamp_decision
        if decision.tzinfo is None:
            decision = decision.replace(tzinfo=timezone.utc)

        elapsed_ms = (now - decision).total_seconds() * 1000.0

        # Decision timestamp too far in the future (client clock skew).
        if elapsed_ms < -self.max_future_skew_ms:
            return (
                False,
                f"future_skew: decision ahead by {-elapsed_ms:.1f}ms > tolerance {self.max_future_skew_ms:.0f}ms",
            )

        if elapsed_ms > intent.freshness_window_ms:
            return False, f"obsolete: elapsed {elapsed_ms:.1f}ms > freshness {intent.freshness_window_ms}ms"

        if intent.deadline_ms is not None and elapsed_ms > intent.deadline_ms:
            return False, f"deadline exceeded: {elapsed_ms:.1f}ms > {intent.deadline_ms}ms"

        return True, "temporal_ok"
