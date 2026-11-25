from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class IntentState(str, Enum):
    """Lifecycle states (paper Fig. 2).

    Validation (t_v) ends in AUTHORIZED, DELAYED, DEGRADED, REVOKED or EXPIRED.
    Authorization, commit, release and effect are distinct steps:

    * AUTHORIZED      validated at t_v; nothing has been released.
    * COMMITTED       the atomic commit (t_c) appended the authorization to the
                      actuation log; still not released.
    * RELEASED        the gateway reports that it handed the command to
                      middleware or to a controller (t_m).
    * WITHHELD        the gate (t_g), the commit, or a release guard refused to
                      release an authorized intent.
    * EFFECT_CONFIRMED / FAILED   the actuator or controller reported that the
                      effect happened (t_a) / did not happen (e.g. a
                      version-conditional command was refused).
    * UNKNOWN_EFFECT  released, but no effect report is available (e.g. a ROS 2
                      publication without feedback); a later report can still
                      resolve it.

    No state means "executed" merely because the intent was authorized or
    committed.
    """

    CREATED = "CREATED"
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    AUTHORIZED = "AUTHORIZED"
    DELAYED = "DELAYED"
    DEGRADED = "DEGRADED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    WITHHELD = "WITHHELD"
    EFFECT_CONFIRMED = "EFFECT_CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN_EFFECT = "UNKNOWN_EFFECT"


class ReleaseDecision(str, Enum):
    """Outcome of the release step, recorded separately from the policy outcome at t_v."""

    RELEASE = "RELEASE"
    WITHHOLD = "WITHHOLD"


class EffectStatus(str, Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


# Degradation policies the runtime implements. AIS accepts no other value, so a
# producer can never declare a fallback that would be ignored.
DEGRADATION_POLICIES = ("none", "reduce_speed")


class DecisionOutcome(str, Enum):
    EXECUTE = "EXECUTE"
    DELAY = "DELAY"
    DEGRADE = "DEGRADE"
    REVOKE = "REVOKE"


@dataclass
class ActionDescriptor:
    action_type: str
    target_entity: str
    parameters: dict[str, Any] = field(default_factory=dict)
    degradation_policy: str = "none"


@dataclass
class ActionIntent:
    id: str
    source: str
    timestamp_decision: datetime
    freshness_window_ms: int
    payload: ActionDescriptor
    deadline_ms: int | None = None
    preconditions: list[str] = field(default_factory=list)
    safety_constraints: list[str] = field(default_factory=list)
    priority: int = 0
    correlation_id: str | None = None
    policy_added: list[str] | None = None  # set once the server-side policy has been applied

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionIntent:
        ts = data["timestamp_decision"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        payload_raw = data["payload"]
        policy = payload_raw.get("degradation_policy", "none")
        if policy not in DEGRADATION_POLICIES:
            # fail closed: a declared fallback the runtime cannot apply is a malformed intent
            raise ValueError(f"unsupported degradation_policy: {policy!r}")
        payload = ActionDescriptor(
            action_type=payload_raw["action_type"],
            target_entity=payload_raw["target_entity"],
            parameters=payload_raw.get("parameters", {}),
            degradation_policy=policy,
        )
        return cls(
            id=data.get("id") or str(uuid4()),
            source=data["source"],
            timestamp_decision=ts,
            freshness_window_ms=int(data["freshness_window_ms"]),
            deadline_ms=data.get("deadline_ms"),
            preconditions=[p.get("expr", p) if isinstance(p, dict) else str(p) for p in data.get("preconditions", [])],
            safety_constraints=[
                p.get("expr", p) if isinstance(p, dict) else str(p) for p in data.get("safety_constraints", [])
            ],
            payload=payload,
            priority=int(data.get("priority", 0)),
            correlation_id=data.get("correlation_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "timestamp_decision": self.timestamp_decision.isoformat(),
            "freshness_window_ms": self.freshness_window_ms,
            "deadline_ms": self.deadline_ms,
            "preconditions": [{"expr": e} for e in self.preconditions],
            "safety_constraints": [{"expr": e} for e in self.safety_constraints],
            "payload": {
                "action_type": self.payload.action_type,
                "target_entity": self.payload.target_entity,
                "parameters": self.payload.parameters,
                "degradation_policy": self.payload.degradation_policy,
            },
            "priority": self.priority,
            "correlation_id": self.correlation_id,
        }


@dataclass
class IntentRecord:
    intent: ActionIntent
    state: IntentState = IntentState.CREATED
    outcome: DecisionOutcome | None = None
    reason: str = ""
    validation_latency_ms: float = 0.0
    context_version: int = 0
    read_set: list[str] = field(default_factory=list)
    read_set_version: int = 0
    policy_predicates: list[str] = field(default_factory=list)
    predicates: list[str] = field(default_factory=list)
    predicate_version: int = -1
    action_class: str = "other"
    # Release step (t_g / t_c / t_m) and effect (t_a), kept apart from ``outcome`` (t_v).
    release_decision: ReleaseDecision | None = None
    commit_seq: int | None = None
    effect_status: EffectStatus | None = None
    effect_detail: str = ""
