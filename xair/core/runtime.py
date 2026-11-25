from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable

from xair.core.context_validator import ContextValidator
from xair.core.coordinator import DistributedCoordinator
from xair.core.execution_decision import ExecutionDecisionEngine
from xair.core.intent_receiver import IntentReceiver
from xair.core.lifecycle import SETTLED_STATES, InvalidTransition, LifecycleTracker
from xair.core.models import (ActionIntent, DecisionOutcome, EffectStatus, IntentRecord, IntentState,
                              ReleaseDecision)
from xair.core.temporal_validator import TemporalValidator
from xair.core.versioning import predicate_version, read_set, read_set_version

ActuationCallback = Callable[[ActionIntent, DecisionOutcome], None]

DEFAULT_IDEMPOTENCY_RETENTION_S = 3600.0
# Action classes, assigned by the operator's policy from the action type and
# never by the producer (AIS has no class field). A ``stop`` reduces risk: no
# predicate, version, age, conflict or store outage may revoke it. The other
# classes only group operator-mandated predicates (``class:<name>`` keys).
ACTION_CLASSES = ("stop", "start", "motion", "reset", "shutdown", "other")
# The stop fence (``stop.<bucket> <= t_d``, see adapters/http_server.py) is a read-set predicate that is
# evaluated at validation but not registered for truth versions: its literal is the decision time of each
# intent, so registering it would add one entry to the registry per intent and make every update slower.
FENCE_PREFIX = "stop."


def _registered(exprs: list[str]) -> list[str]:
    return [e for e in exprs if not e.lstrip().startswith(FENCE_PREFIX)]
RISK_REDUCING = frozenset({"stop"})
_VALIDATABLE_STATES = frozenset({IntentState.CREATED, IntentState.DELAYED, IntentState.DEGRADED})


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (the definition used for every reported figure)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered) - 1e-9))
    return ordered[min(rank, len(ordered)) - 1]


class XAIRRuntime:
    """Orchestrates intent reception, validation, decision, and lifecycle.

    All mutations of shared state (lifecycle records, idempotency table,
    resource locks, queue) happen under one re-entrant lock, so concurrent
    HTTP workers observe a serializable sequence of admissions and decisions.
    Predicate evaluation itself is stateless and runs on the snapshot the
    caller passes in.
    """

    def __init__(
        self,
        context: dict | None = None,
        on_actuation: ActuationCallback | None = None,
        *,
        idempotency_retention_s: float = DEFAULT_IDEMPOTENCY_RETENTION_S,
    ) -> None:
        self.receiver = IntentReceiver()
        self.temporal = TemporalValidator()
        self.context = ContextValidator(context)
        self.decision_engine = ExecutionDecisionEngine()
        self.coordinator = DistributedCoordinator()
        self.lifecycle = LifecycleTracker()
        self.on_actuation = on_actuation
        self.idempotency_retention_s = idempotency_retention_s
        self._lock = threading.RLock()
        self._context_version = 0
        self._metrics = {
            "intents_received": 0,
            "authorized": 0,
            "committed": 0,
            "released": 0,
            "withheld": 0,
            "effect_confirmed": 0,
            "effect_failed": 0,
            "effect_unknown": 0,
            "revoked": 0,
            "delayed": 0,
            "degraded": 0,
            "validation_latencies_ms": [],
        }
        # intent id -> monotonic admission time, oldest first.
        self._seen: OrderedDict[str, float] = OrderedDict()
        # Server-side policy: action_type -> predicates every such intent must
        # satisfy, whatever its producer declared (XAIR_POLICY_FILE, JSON).
        self.policy: dict[str, list[str]] = {}
        self.action_classes: dict[str, str] = {}
        self.class_policy: dict[str, list[str]] = {}
        if path := os.environ.get("XAIR_POLICY_FILE"):
            self.set_policy(json.loads(open(path, encoding="utf-8").read()))

    # ----------------------------------------------------------------- policy

    def set_policy(self, policy: dict) -> dict[str, list[str]]:
        """Install the operator's policy.

        ``{"RESUME": ["line.state == 'RUN'"]}`` makes predicates mandatory for an
        action type; ``{"STOP": {"class": "stop"}, "JOG": {"class": "motion",
        "require": [...]}}`` also assigns its class, and ``{"class:reset": [...]}``
        makes predicates mandatory for every action of a class. A stop-class
        action cannot carry mandatory predicates. Action types are matched
        case-insensitively and without surrounding whitespace, so a producer
        cannot bypass the policy by spelling. Raises ValueError on a bad policy
        (the previous one stays installed)."""
        actions: dict[str, list[str]] = {}
        classes: dict[str, str] = {}
        class_preds: dict[str, list[str]] = {}
        for key, value in (policy or {}).items():
            key = str(key).strip()
            if key.lower().startswith("class:"):
                name = key[6:].strip().lower()
                if name not in ACTION_CLASSES:
                    raise ValueError(f"unknown action class: {name}")
                if name in RISK_REDUCING and value:
                    raise ValueError(f"class {name} cannot carry mandatory predicates")
                class_preds[name] = [str(e) for e in value]
                continue
            if isinstance(value, dict):
                if set(value) - {"class", "require"}:
                    raise ValueError(f"{key}: only 'class' and 'require' are allowed")
                cls, require = str(value.get("class", "other")).strip().lower(), value.get("require", [])
            else:
                cls, require = "other", value
            if cls not in ACTION_CLASSES:
                raise ValueError(f"{key}: unknown action class {cls}")
            if cls in RISK_REDUCING and require:
                raise ValueError(f"{key}: a {cls}-class action cannot carry mandatory predicates")
            actions[key.upper()] = [str(e) for e in require]
            classes[key.upper()] = cls
        with self._lock:
            self.policy, self.action_classes, self.class_policy = actions, classes, class_preds
            return dict(self.policy)

    def action_class(self, intent: ActionIntent) -> str:
        """The class the operator's policy assigns to the intent's action type ("other" if none)."""
        return self.action_classes.get(str(intent.payload.action_type).strip().upper(), "other")

    def policy_doc(self) -> dict:
        return {"policy": dict(self.policy), "action_classes": dict(self.action_classes),
                "class_policy": dict(self.class_policy)}

    def _apply_policy(self, intent: ActionIntent) -> list[str]:
        """Append the mandatory predicates the producer omitted (once); return the ones added."""
        if intent.policy_added is None:
            cls = self.action_class(intent)
            mandated = [*self.policy.get(str(intent.payload.action_type).strip().upper(), []),
                        *self.class_policy.get(cls, [])]
            added = list(dict.fromkeys(e for e in mandated
                                       if e not in intent.preconditions and e not in intent.safety_constraints))
            intent.preconditions = [*intent.preconditions, *added]
            intent.policy_added = added
        return list(intent.policy_added)

    def prepare(self, intent: ActionIntent) -> list[str]:
        """Apply the server-side policy and return every predicate the intent will be checked against."""
        with self._lock:
            self._apply_policy(intent)
        return _registered([*intent.safety_constraints, *intent.preconditions])

    # ------------------------------------------------------------------ context

    def install_context_snapshot(self, snapshot: dict, version: int, *, replace: bool = False) -> int:
        """Install a snapshot unless it is older than the one already installed."""
        with self._lock:
            if version < self._context_version:
                return self._context_version
            self._context_version = version
            if replace:
                self.context.replace_context(snapshot)
            else:
                self.context.update_context(snapshot)
            return self._context_version

    def update_context(self, context: dict) -> None:
        with self._lock:
            self.context.update_context(context)

    # -------------------------------------------------------------- admission

    def _expire_seen(self) -> None:
        horizon = time.monotonic() - self.idempotency_retention_s
        while self._seen:
            intent_id, admitted_at = next(iter(self._seen.items()))
            if admitted_at >= horizon:
                break
            self._seen.popitem(last=False)
            record = self.lifecycle.get(intent_id)
            if record is not None and record.state in SETTLED_STATES:
                self.lifecycle.forget(intent_id)

    def is_duplicate(self, intent_id: str) -> bool:
        with self._lock:
            self._expire_seen()
            return intent_id in self._seen

    def admit(self, intent: ActionIntent, *, enqueue: bool = False) -> tuple[IntentRecord, bool]:
        """Register an intent once per retention window; return (record, duplicate)."""
        with self._lock:
            self._expire_seen()
            if intent.id in self._seen:
                record = self.lifecycle.get(intent.id)
                if record is not None:
                    return record, True
            self._seen[intent.id] = time.monotonic()
            self._metrics["intents_received"] += 1
            record = self.lifecycle.register(intent)
            if enqueue:
                self.receiver.submit(intent)
            return record, False

    def submit_intent(self, intent: ActionIntent) -> IntentRecord:
        """Admit and enqueue for a later process_next() pass."""
        record, _ = self.admit(intent, enqueue=True)
        return record

    def submit_and_process(
        self,
        intent: ActionIntent,
        *,
        context_snapshot: dict | None = None,
        context_version: int | None = None,
        now: datetime | None = None,
    ) -> tuple[IntentRecord, bool]:
        with self._lock:
            if context_snapshot is not None and context_version is not None:
                self.install_context_snapshot(context_snapshot, context_version)
            record, duplicate = self.admit(intent)
            if duplicate:
                return record, True
            return self.process_intent(intent, now=now, context_version=context_version), False

    # ------------------------------------------------------------- validation

    def process_next(self, now: datetime | None = None) -> IntentRecord | None:
        with self._lock:
            intent = self.receiver.pop()
            if intent is None:
                return None
            return self.process_intent(intent, now=now)

    def process_intent(
        self,
        intent: ActionIntent,
        now: datetime | None = None,
        *,
        context: dict | None = None,
        context_version: int | None = None,
        path_versions: dict[str, int] | None = None,
        predicate_versions: dict[str, list] | None = None,
        requeue: bool = True,
    ) -> IntentRecord:
        """Validate one intent at t_v.

        ``context``/``context_version`` are the snapshot the caller read
        atomically from the store; when omitted, the installed snapshot is
        used. A snapshot older than the installed one is rejected as stale.
        With ``requeue=False`` a DELAYED or DEGRADED intent is not put back on
        the internal queue: the caller revalidates it itself (HTTP ingress).
        """
        with self._lock:
            record = self.lifecycle.get(intent.id) or self.lifecycle.register(intent)
            if record.state not in _VALIDATABLE_STATES:
                # Terminal, or already authorized and awaiting its release report:
                # a second validation pass must not re-acquire the target.
                return record
            now = now or datetime.now(timezone.utc)
            t0 = time.perf_counter()
            installed = self._context_version
            if context is None:
                snapshot, version = self.context.context, installed
            else:
                snapshot, version = context, (installed if context_version is None else context_version)

            self.lifecycle.transition(intent.id, IntentState.PENDING)
            self.lifecycle.transition(intent.id, IntentState.VALIDATING)
            record.action_class = self.action_class(intent)
            if record.action_class in RISK_REDUCING:
                # A stop is authorized on schema validity alone: its declared
                # predicates are kept for the audit but never evaluated, it takes
                # no target lock, and neither age nor a stale snapshot revokes it.
                intent.policy_added = []
                record.intent, record.context_version = intent, installed
                record.read_set, record.read_set_version, record.predicates = [], 0, []
                record.validation_latency_ms = (time.perf_counter() - t0) * 1000.0
                self.lifecycle.transition(intent.id, IntentState.AUTHORIZED, DecisionOutcome.EXECUTE,
                                          "risk_reducing_action", record.validation_latency_ms)
                self._metrics["authorized"] += 1
                return record
            if context_version is not None and context_version < installed:
                record.context_version = installed
                self.lifecycle.transition(
                    intent.id, IntentState.REVOKED, DecisionOutcome.REVOKE, "stale_context_snapshot"
                )
                self._metrics["revoked"] += 1
                return record

            record.policy_predicates = self._apply_policy(intent)
            conflict, _ = self.coordinator.check_conflict(intent)
            temporal_ok, temporal_reason = self.temporal.validate(intent, now)
            context_ok, context_reason = self.context.validate(intent, snapshot)
            outcome, reason = self.decision_engine.decide(
                intent, temporal_ok, temporal_reason, context_ok, context_reason, resource_busy=conflict
            )

            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._metrics["validation_latencies_ms"].append(latency_ms)
            record.context_version = version
            record.read_set = read_set([*intent.safety_constraints, *intent.preconditions])
            record.read_set_version = (
                read_set_version(path_versions, record.read_set) if path_versions is not None else version
            )
            record.predicates = _registered([*intent.safety_constraints, *intent.preconditions])
            record.predicate_version = (
                predicate_version(predicate_versions, record.predicates) if predicate_versions is not None else -1
            )
            record.validation_latency_ms = latency_ms

            if outcome == DecisionOutcome.DEGRADE:
                # Transform the payload, clear the degradation policy, and return
                # the *same* identifier to the pending queue for a fresh pass.
                record.intent = self._apply_degradation(intent)
                self.lifecycle.transition(intent.id, IntentState.DEGRADED, outcome, reason, latency_ms)
                self._metrics["degraded"] += 1
                if requeue:
                    self.receiver.submit(record.intent)
            elif outcome == DecisionOutcome.EXECUTE:
                self.coordinator.acquire(intent)
                record.intent = intent
                self.lifecycle.transition(intent.id, IntentState.AUTHORIZED, outcome, reason, latency_ms)
                self._metrics["authorized"] += 1
            elif outcome == DecisionOutcome.DELAY:
                self.lifecycle.transition(intent.id, IntentState.DELAYED, outcome, reason, latency_ms)
                self._metrics["delayed"] += 1
                if requeue:
                    self.receiver.submit(intent)
            else:
                state = IntentState.EXPIRED if reason.startswith("deadline") else IntentState.REVOKED
                self.lifecycle.transition(intent.id, state, outcome, reason, latency_ms)
                self._metrics["revoked"] += 1
            return record

    def process_all(self, now: datetime | None = None) -> list[IntentRecord]:
        results = []
        while (r := self.process_next(now=now)) is not None:
            results.append(r)
        return results

    # ------------------------------------------------- commit, release, effect

    def _reject(self, record: IntentRecord, requested: IntentState, reason: str) -> InvalidTransition:
        """Audit a report the FSM refuses (e.g. a release reported after a supervisory revoke)."""
        self.lifecycle.note(record.intent.id, f"rejected_report:{requested.value}:{reason}")
        return InvalidTransition(record.intent.id, record.state, requested)

    def record_commit(self, intent_id: str, committed: bool, reason: str, *, seq: int | None = None) -> IntentRecord:
        """Record the outcome of the atomic authorization commit (t_c).

        A committed intent is COMMITTED, not released: the gateway still has to
        hand the command to middleware (t_m) and report it. A refused commit
        withholds the intent. The target lock taken at t_v is released here, as
        the actuation log, not the lock, orders committed authorizations."""
        with self._lock:
            record = self.lifecycle.get(intent_id)
            if record is None:
                raise KeyError(intent_id)
            target = IntentState.COMMITTED if committed else IntentState.WITHHELD
            if record.state == target and (committed or record.release_decision == ReleaseDecision.WITHHOLD):
                return record  # idempotent replay of the same outcome
            if record.state != IntentState.AUTHORIZED:
                raise self._reject(record, target, reason)
            self.coordinator.release(record.intent)
            if committed:
                record.commit_seq = seq
                self.lifecycle.transition(intent_id, IntentState.COMMITTED, reason=reason)
                self._metrics["committed"] += 1
            else:
                record.release_decision = ReleaseDecision.WITHHOLD
                self.lifecycle.transition(intent_id, IntentState.WITHHELD, reason=reason)
                self._metrics["withheld"] += 1
            return record

    def report_release(
        self,
        intent_id: str,
        released: bool,
        reason: str,
        *,
        context_version: int | None = None,
        read_set_version: int | None = None,
    ) -> IntentRecord:
        """Record the gateway's release decision (t_g/t_m).

        From AUTHORIZED (optimistic mode) the gateway reports the version it
        observed at t_g: the read-set version (default scope) or the global
        version. A version that differs from the value recorded at t_v turns a
        reported release into WITHHELD. From COMMITTED (atomic mode) the report
        says whether the committed command was handed to middleware, or
        withheld by a release guard. A replay of the same decision is
        idempotent; a conflicting one is rejected and audited."""
        with self._lock:
            record = self.lifecycle.get(intent_id)
            if record is None:
                raise KeyError(intent_id)
            wanted = ReleaseDecision.RELEASE if released else ReleaseDecision.WITHHOLD
            if not released and record.state == IntentState.REVOKED:
                return record  # the gate withheld an intent already revoked (e.g. supervisory): nothing to record
            if record.release_decision is not None:
                if record.release_decision == wanted:
                    return record
                raise self._reject(record, IntentState.RELEASED if released else IntentState.WITHHELD, reason)
            if record.state not in (IntentState.AUTHORIZED, IntentState.COMMITTED):
                raise self._reject(record, IntentState.RELEASED if released else IntentState.WITHHELD, reason)
            if record.state == IntentState.AUTHORIZED and released:
                if read_set_version is not None and read_set_version != record.read_set_version:
                    released, reason = False, "read_set_version_changed_at_gate"
                elif read_set_version is None and context_version is not None and context_version != record.context_version:
                    released, reason = False, "context_version_changed_at_publish"
            self.coordinator.release(record.intent)
            if not released:
                record.release_decision = ReleaseDecision.WITHHOLD
                self.lifecycle.transition(intent_id, IntentState.WITHHELD, reason=reason)
                self._metrics["withheld"] += 1
                return record
            record.release_decision = ReleaseDecision.RELEASE
            self.lifecycle.transition(intent_id, IntentState.RELEASED, reason=reason)
            self._metrics["released"] += 1
            if self.on_actuation:
                self.on_actuation(record.intent, DecisionOutcome.EXECUTE)
            return record

    def confirm_publication(self, intent_id: str, publish: bool, reason: str, **versions) -> IntentRecord:
        """Deprecated alias of :meth:`report_release` (the former publication report)."""
        return self.report_release(intent_id, publish, reason, **versions)

    def report_effect(self, intent_id: str, status: EffectStatus | str, detail: str = "") -> IntentRecord:
        """Record what the actuator or controller reported about the effect (t_a).

        Only a released intent can have an effect. ``unknown`` records that no
        feedback exists; a later ``confirmed`` or ``failed`` report resolves
        it. A replay of the same status is idempotent."""
        status = EffectStatus(status)
        target = {EffectStatus.CONFIRMED: IntentState.EFFECT_CONFIRMED, EffectStatus.FAILED: IntentState.FAILED,
                  EffectStatus.UNKNOWN: IntentState.UNKNOWN_EFFECT}[status]
        with self._lock:
            record = self.lifecycle.get(intent_id)
            if record is None:
                raise KeyError(intent_id)
            if record.state == target:
                return record
            if target not in self.lifecycle.allowed(record.state):
                raise self._reject(record, target, detail)
            record.effect_status, record.effect_detail = status, detail
            self.lifecycle.transition(intent_id, target, reason=detail or status.value)
            self._metrics[{EffectStatus.CONFIRMED: "effect_confirmed", EffectStatus.FAILED: "effect_failed",
                           EffectStatus.UNKNOWN: "effect_unknown"}[status]] += 1
            return record

    def supervisory_revoke(self, intent_id: str, reason: str = "human_supervisory_revoke") -> IntentRecord:
        """Revoke an intent that has not been committed or released yet."""
        with self._lock:
            record = self.lifecycle.get(intent_id)
            if record is None:
                raise KeyError(intent_id)
            self.lifecycle.transition(intent_id, IntentState.REVOKED, DecisionOutcome.REVOKE, reason)
            self.coordinator.release(record.intent)
            return record

    # ---------------------------------------------------------------- helpers

    def _apply_degradation(self, intent: ActionIntent) -> ActionIntent:
        """Transform the payload and clear the degradation policy for same-intent revalidation.

        Only ``reduce_speed`` is implemented (it halves ``parameters.speed_factor``,
        default 1.0). AIS admits no other policy, so a declared fallback is never
        silently replaced by the original payload."""
        policy = intent.payload.degradation_policy
        if policy != "reduce_speed":
            raise ValueError(f"unsupported degradation_policy: {policy!r}")
        params = dict(intent.payload.parameters)
        params["speed_factor"] = float(params.get("speed_factor", 1.0)) * 0.5
        intent.payload.parameters = params
        intent.payload.degradation_policy = "none"
        return intent

    def get_metrics(self) -> dict:
        with self._lock:
            lat = list(self._metrics["validation_latencies_ms"])
            decided = self._metrics["authorized"] + self._metrics["degraded"] + self._metrics["revoked"]
            return {
                **{k: v for k, v in self._metrics.items() if k != "validation_latencies_ms"},
                "validation_latency_p50_ms": percentile(lat, 0.50),
                "validation_latency_p99_ms": percentile(lat, 0.99),
                # Share of decided intents that were revoked at t_v. This is an
                # operational counter, not SER: SER needs ground-truth labels.
                "revoke_fraction": self._metrics["revoked"] / decided if decided else 0.0,
            }
