from __future__ import annotations

import os
from datetime import datetime, timezone

from xair.core.models import ActionIntent


class TemporalValidator:
    """Temporal admissibility of an intent, measured from its decision time t_d.

    Two bounds with distinct roles:

    * the freshness window ``w`` bounds the age of the decision when it is
      *validated* (t_v): a decision older than ``w`` is not evaluated at all;
    * the deadline ``d`` bounds the age at *release* (the gate recheck and the
      atomic commit); without a deadline, ``w`` bounds the release as well.

    Ages compare the checking process's UTC clock with the producer's t_d, so
    they carry the offset between the two clocks. ``clock_uncertainty_ms``
    (epsilon, default 0) is the declared bound on that offset; it is added to
    every measured age, so an accepted intent satisfies its bound on the true
    age whenever the offset is within epsilon. Timestamps more than
    ``max_future_skew_ms`` ahead of the server clock are rejected.
    """

    def __init__(self, max_future_skew_ms: float = 1000.0, clock_uncertainty_ms: float | None = None) -> None:
        self.max_future_skew_ms = max_future_skew_ms
        self.clock_uncertainty_ms = (
            float(os.environ.get("XAIR_CLOCK_UNCERTAINTY_MS", "0"))
            if clock_uncertainty_ms is None else float(clock_uncertainty_ms)
        )

    @staticmethod
    def decision_epoch_ms(intent: ActionIntent) -> float:
        decision = intent.timestamp_decision
        if decision.tzinfo is None:
            decision = decision.replace(tzinfo=timezone.utc)
        return decision.timestamp() * 1000.0

    @staticmethod
    def release_bound_ms(intent: ActionIntent) -> float:
        """Maximum age at release: the deadline if declared, else the freshness window."""
        return float(intent.deadline_ms if intent.deadline_ms is not None else intent.freshness_window_ms)

    def _age_ms(self, intent: ActionIntent, now: datetime | None) -> float:
        now = now or datetime.now(timezone.utc)
        return now.timestamp() * 1000.0 - self.decision_epoch_ms(intent)

    def _future_skew(self, age_ms: float) -> str | None:
        if age_ms < -self.max_future_skew_ms:
            return f"future_skew: decision ahead by {-age_ms:.1f}ms > tolerance {self.max_future_skew_ms:.0f}ms"
        return None

    def validate(self, intent: ActionIntent, now: datetime | None = None) -> tuple[bool, str]:
        """Admissibility at validation (t_v): freshness window and, if declared, deadline."""
        age = self._age_ms(intent, now)
        if (skew := self._future_skew(age)) is not None:
            return False, skew
        age += self.clock_uncertainty_ms
        if age > intent.freshness_window_ms:
            return False, f"obsolete: elapsed {age:.1f}ms > freshness {intent.freshness_window_ms}ms"
        if intent.deadline_ms is not None and age > intent.deadline_ms:
            return False, f"deadline exceeded: {age:.1f}ms > {intent.deadline_ms}ms"
        return True, "temporal_ok"

    def validate_release(self, intent: ActionIntent, now: datetime | None = None) -> tuple[bool, str]:
        """Admissibility at release (t_g, or the atomic commit): the release bound only."""
        age = self._age_ms(intent, now)
        if (skew := self._future_skew(age)) is not None:
            return False, skew
        age += self.clock_uncertainty_ms
        bound = self.release_bound_ms(intent)
        if age > bound:
            kind = "deadline exceeded" if intent.deadline_ms is not None else "obsolete"
            return False, f"{kind}: elapsed {age:.1f}ms > {bound:.0f}ms at release"
        return True, "temporal_ok"
