"""Actuator-side consumer of the committed actuation log (transactional outbox).

In the atomic mode the authorization commit (t_c) appends a record to the
actuation log in the same store operation that checks the read-set version.
This consumer is the only path from that log to an actuator: it reads records
in commit order from a durable offset, applies each one, and advances the
offset. Delivery is at least once (a crash between the effect and the offset
update replays the record); the actuator deduplicates by intent id, so each
committed authorization has at most one effect.

At apply time (t_a) the consumer compares the current read-set (or
predicate-truth) version with the one recorded at the commit, and the age of
the decision with its release bound. A difference is a *post-commit invalidation*:
context changed in (t_c, t_a]. By default (``recheck_at_apply=True``, fail
closed) the command is withheld, which narrows the window to the consumer's own
read-to-apply interval; with ``recheck_at_apply=False`` it is reported and the
command is applied anyway (the authorization was valid at its commit), which is
only appropriate for commands that cannot increase activity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from xair.core.context_store import RedisContextStore
from xair.core.versioning import predicate_version, read_set_version


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
                 *, recheck_at_apply: bool = True,
                 after_apply_hook: Callable[[int, dict], None] | None = None) -> None:
        self.store, self.actuator, self.name = store, actuator, name
        self.recheck_at_apply = recheck_at_apply
        self.after_apply_hook = after_apply_hook
        self.offset_key = f"xair:consumer:{name}:offset"
        self.applied: list[dict] = []
        self.invalidated_in_flight: list[str] = []
        self.withheld: list[str] = []
        self.late_at_apply: list[str] = []

    @property
    def offset(self) -> int:
        raw = self.store.kv_get(self.offset_key)
        return int(raw) if raw else 0

    def poll(self) -> int:
        """Apply every record committed after the durable offset; return how many were read."""
        start = self.offset
        records = self.store.actuation_log(start)
        for i, record in enumerate(records):
            if record.get("withheld") or "commit_version" not in record:
                # tombstones and other non-authorizing entries carry no command
                self.store.kv_set(self.offset_key, str(start + i + 1))
                continue
            exprs = record.get("predicates") or []
            _, _, pv, trusted, _, preds = self.store.snapshot_with_predicates(exprs if record.get("scope") == "truth" else [])
            if not trusted:
                current = None
            elif record.get("scope") == "truth":
                current = predicate_version(preds, exprs)
            else:
                current = read_set_version(pv, record.get("read_set") or [])
            expected = record.get("predicate_version") if record.get("scope") == "truth" else record.get("read_set_version")
            moved = current is None or current != expected
            late = False
            if record.get("decision_epoch_ms") is not None and record.get("release_bound_ms") is not None:
                late = time.time() * 1000.0 - float(record["decision_epoch_ms"]) > float(record["release_bound_ms"])
            if moved:
                self.invalidated_in_flight.append(record["intent_id"])
            if late:
                self.late_at_apply.append(record["intent_id"])
            if (moved or late) and self.recheck_at_apply:
                self.withheld.append(record["intent_id"])
            elif self.actuator.apply(record):
                self.applied.append({**record, "t_apply_mono_ms": time.perf_counter() * 1000.0})
            if self.after_apply_hook is not None:
                self.after_apply_hook(start + i, record)  # may raise ConsumerCrash before the offset moves
            self.store.kv_set(self.offset_key, str(start + i + 1))
        return len(records)


class SqliteActuator:
    """Actuator stub whose idempotency record survives process crashes (SQLite, synchronous commits).

    Each effect is recorded in the same local transaction that marks the intent
    as done, standing in for a device that keeps the id of the last commands it
    executed in non-volatile memory."""

    def __init__(self, path: str) -> None:
        import sqlite3
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS done (intent_id TEXT PRIMARY KEY, t REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts (intent_id TEXT, t REAL)")
        self.suppressed_duplicates = 0

    def apply(self, record: dict) -> bool:
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("INSERT INTO attempts VALUES (?, ?)", (record["intent_id"], now))
        cur = self.db.execute("INSERT OR IGNORE INTO done VALUES (?, ?)", (record["intent_id"], now))
        self.db.execute("COMMIT")
        if cur.rowcount == 0:
            self.suppressed_duplicates += 1
            return False
        return True


def main() -> int:
    """Run a consumer process: python -m xair.adapters.actuation_consumer --redis URL --db actuator.sqlite"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--name", default="actuator")
    ap.add_argument("--poll-ms", type=float, default=5.0)
    args = ap.parse_args()
    store = RedisContextStore(args.redis)
    consumer = ActuationConsumer(store, SqliteActuator(args.db), args.name)  # type: ignore[arg-type]
    while True:
        try:
            if consumer.poll() == 0:
                time.sleep(args.poll_ms / 1000.0)
        except Exception:
            time.sleep(0.05)  # store unreachable (e.g. restarting): retry


if __name__ == "__main__":
    raise SystemExit(main())
