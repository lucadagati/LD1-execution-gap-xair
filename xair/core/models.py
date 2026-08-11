from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class IntentState(str, Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    EXECUTED = "EXECUTED"
    DELAYED = "DELAYED"
    DEGRADED = "DEGRADED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


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
    revocable: bool = True
    correlation_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionIntent:
        ts = data["timestamp_decision"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        payload_raw = data["payload"]
        payload = ActionDescriptor(
            action_type=payload_raw["action_type"],
            target_entity=payload_raw["target_entity"],
            parameters=payload_raw.get("parameters", {}),
            degradation_policy=payload_raw.get("degradation_policy", "none"),
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
            revocable=bool(data.get("revocable", True)),
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
            "revocable": self.revocable,
            "correlation_id": self.correlation_id,
        }


@dataclass
class IntentRecord:
    intent: ActionIntent
    state: IntentState = IntentState.CREATED
    outcome: DecisionOutcome | None = None
    reason: str = ""
    validation_latency_ms: float = 0.0
