"""Actuator-side consumer of the committed actuation log (transactional outbox).

In the atomic mode the authorization commit (t_c) appends a record to the
actuation log in the same store operation that checks the read-set version.
This consumer is the only path from that log to an actuator: it reads records
in commit order from a durable offset, applies each one, and advances the
offset. Delivery is at least once (a crash between the effect and the offset
update replays the record); the actuator deduplicates by intent id, so each
committed authorization has at most one effect.

At apply time (t_a) the consumer compares the current read-set version with
the one recorded at the commit. A difference is a *post-commit invalidation*:
context changed in (t_c, t_a]. By default it is reported and the command is
applied (the authorization was valid at its commit); with
``recheck_at_apply=True`` the command is withheld instead, which narrows the
window again to the consumer's own read-to-apply interval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from xair.core.context_store import RedisContextStore
from xair.core.versioning import read_set_version


class ConsumerCrash(RuntimeError):
    """Raised by a fault-injection hook to emulate a consumer process crash."""


@dataclass
class IdempotentActuator:
    """Actuator stub that performs each intent's effect at most once."""

    effects: list[str] = field(default_factory=list)
    _done: set[str] = field(default_factory=set)
    suppressed_duplicates: int = 0

    def apply(self, record: dict) -> bool:
        intent_id = record["intent_id"]
        if intent_id in self._done:
            self.suppressed_duplicates += 1
            return False
        self._done.add(intent_id)
        self.effects.append(intent_id)
        return True


class ActuationConsumer:
    def __init__(self, store: RedisContextStore, actuator: IdempotentActuator, name: str = "default",
                 *, recheck_at_apply: bool = False,
                 after_apply_hook: Callable[[int, dict], None] | None = None) -> None:
        self.store, self.actuator, self.name = store, actuator, name
        self.recheck_at_apply = recheck_at_apply
        self.after_apply_hook = after_apply_hook
        self.offset_key = f"xair:consumer:{name}:offset"
        self.applied: list[dict] = []
        self.invalidated_in_flight: list[str] = []
        self.withheld: list[str] = []

    @property
    def offset(self) -> int:
        raw = self.store.kv_get(self.offset_key)
        return int(raw) if raw else 0

    def poll(self) -> int:
        """Apply every record committed after the durable offset; return how many were read."""
        start = self.offset
        records = self.store.actuation_log(start)
        for i, record in enumerate(records):
            _, _, pv, trusted = self.store.snapshot_full()
            current = read_set_version(pv, record.get("read_set") or []) if trusted else None
            moved = current is None or current != record.get("read_set_version")
            if moved:
                self.invalidated_in_flight.append(record["intent_id"])
            if moved and self.recheck_at_apply:
                self.withheld.append(record["intent_id"])
            elif self.actuator.apply(record):
                self.applied.append({**record, "t_apply_mono_ms": time.perf_counter() * 1000.0})
            if self.after_apply_hook is not None:
                self.after_apply_hook(start + i, record)  # may raise ConsumerCrash before the offset moves
            self.store.kv_set(self.offset_key, str(start + i + 1))
        return len(records)
