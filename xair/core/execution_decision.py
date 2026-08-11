from __future__ import annotations

from xair.core.models import ActionIntent, DecisionOutcome


class ExecutionDecisionEngine:
    """Map validation results to EXECUTE | DELAY | DEGRADE | REVOKE."""

    def decide(
        self,
        intent: ActionIntent,
        temporal_ok: bool,
        temporal_reason: str,
        context_ok: bool,
        context_reason: str,
        resource_busy: bool = False,
    ) -> tuple[DecisionOutcome, str]:
        if not temporal_ok:
            return DecisionOutcome.REVOKE, temporal_reason

        if not context_ok:
            if resource_busy:
                return DecisionOutcome.DELAY, context_reason
            return DecisionOutcome.REVOKE, context_reason

        if intent.payload.degradation_policy != "none":
            return DecisionOutcome.DEGRADE, "degradation_policy_active"

        return DecisionOutcome.EXECUTE, "valid"
