#!/usr/bin/env python3
"""
Actuator gateway: WebSocket + HTTP -> XAIR (HTTP API) -> ROS 2.

Endpoints: POST /command (legacy XR pose), POST /intent (AIS), POST /context
(line state), GET /health. The adapter policy is selected per request with
``?mode=`` (default ``$XAIR_VALIDATION_MODE`` or ``xair``):

  direct               publish without any check (failure floor)
  naive                freshness/deadline only (failure floor)
  local                refresh the adapter cache from XAIR, then validate locally
  local_stale          validate against the adapter cache, never refreshed
  local_push           refresh only if ``push_notified=true`` (emulated push)
  local_authoritative  read the shared snapshot, validate, recheck version at t_g
  xair                 central validation at t_v + optimistic gate recheck at t_g
  xair_atomic          central validation at t_v + atomic authorization commit in the store
  opa                  Open Policy Agent decides the predicates at arrival and before release
                       (``version_scope=readset`` adds the read-set version condition)

At the controller (``actuator=plc_*`` / ``openplc_*``): unconditional, local interlock
(``*_predicate``), or version-conditional (``*_conditional``) command.

Every contextual mode uses the same temporal validator and predicate
evaluator as XAIR (``xair.core``), so policies differ only in *where* the
context is read, which is the variable the evaluation ablates.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_SCRIPTS = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS.parent
for _p in (str(_REPO_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from xair.core.context_validator import evaluate_predicates  # noqa: E402
from xair.core.deep_merge import deep_merge  # noqa: E402
from xair.core.models import ActionIntent  # noqa: E402
from xair.core.temporal_validator import TemporalValidator  # noqa: E402
from xair.core.versioning import _MISSING, VERSION_SCOPES, get_path, predicate_version, read_set, read_set_version  # noqa: E402
from xair_http_client import XAIRHttpClient  # noqa: E402

try:
    import websockets
except ImportError:
    websockets = None

XAIR = XAIRHttpClient()
DEFAULT_MODE = os.environ.get("XAIR_VALIDATION_MODE", "xair")
# Which version the t_g recheck compares: "readset" (latest value change on the
# paths the intent's predicates read) or "global" (any accepted context update).
DEFAULT_VERSION_SCOPE = os.environ.get("XAIR_VERSION_SCOPE", "readset")
_TEMPORAL = TemporalValidator()

# Adapter-local context cache. Only POST /context and the explicit refreshes
# of the `local` / `local_push` policies write it; `local_authoritative` and
# `xair` evaluate on the snapshot they read and never touch it, so running one
# policy cannot silently refresh the cache another policy relies on.
_cache_lock = threading.Lock()
_context_cache: dict = {}

_node = _pub_pose = _pub_gripper = _Pose = _Point = None
_counter_lock = threading.Lock()
_ros_publish_count = 0
_gateway_release_count = 0


# --------------------------------------------------------------------- cache

def _cache_snapshot() -> dict:
    with _cache_lock:
        return dict(_context_cache)


def _cache_merge(patch: dict) -> None:
    global _context_cache
    with _cache_lock:
        _context_cache = deep_merge(_context_cache, patch)


# ---------------------------------------------------------------- validation

def _intent_or_none(data: dict) -> ActionIntent | None:
    try:
        return ActionIntent.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _temporal_ok(intent: ActionIntent, release: bool = False) -> tuple[bool, str]:
    # Freshness bounds the age at validation; at a release recheck the bound is
    # the deadline (or the freshness window when no deadline is declared).
    return _TEMPORAL.validate_release(intent) if release else _TEMPORAL.validate(intent)


def _predicates_ok(intent: ActionIntent, context: dict) -> tuple[bool, str]:
    return evaluate_predicates(intent.safety_constraints, intent.preconditions, context)


def _validate(intent: ActionIntent, context: dict, release: bool = False) -> tuple[bool, str]:
    ok, reason = _temporal_ok(intent, release)
    if not ok:
        return ok, reason
    return _predicates_ok(intent, context)


def _pull_xair_context() -> tuple[dict, int | None, bool]:
    ctx, ver, _, trusted = _pull_xair_snapshot()
    return ctx, ver, trusted


def _pull_xair_snapshot() -> tuple[dict, int | None, dict[str, int], bool]:
    return _pull_xair_snapshot_timed()[:4]


def _pull_xair_snapshot_timed(predicates: list[str] | None = None, intent_id: str | None = None):
    """(context, version, path_versions, trusted, store-read monotonic bounds or None, predicate_versions,
    lifecycle state of ``intent_id`` or None)."""
    try:
        snap = XAIR.get_context(predicates, intent_id) if intent_id is not None else XAIR.get_context(predicates)
        ver = snap.get("context_version")
        return ((snap.get("context") or {}), (int(ver) if ver is not None else None),
                {k: int(v) for k, v in (snap.get("path_versions") or {}).items()},
                bool(snap.get("context_trusted", False)), snap.get("store_read_mono_ms"),
                snap.get("predicate_versions") or {}, snap.get("intent_state"))
    except Exception:
        return {}, None, {}, False, None, {}, None


def _scope(query: dict) -> str:
    scope = str(query.get("version_scope", DEFAULT_VERSION_SCOPE)).lower()
    return scope if scope in VERSION_SCOPES else "readset"


def _gate_version(scope: str, ver: int | None, pv: dict[str, int], paths: list[str]) -> int | None:
    """Version compared at t_g under ``scope`` (global snapshot or intent read-set)."""
    if scope == "global":
        return ver
    return read_set_version(pv, paths) if ver is not None else None


# ---------------------------------------------------------------------- ROS

def setup_ros():
    try:
        import rclpy
        from geometry_msgs.msg import Point, Pose
        return rclpy, Pose, Point
    except ImportError:
        return None, None, None


def main_ros():
    rclpy, Pose, Point = setup_ros()
    if rclpy is None:
        return None, None, None, None, None
    try:
        rclpy.init()
    except RuntimeError:
        # Witness or another node may have initialized the context already.
        pass
    node = rclpy.create_node("actuator_gateway")
    pub_pose = node.create_publisher(Pose, "/UE_TCP_position", 10)
    pub_gripper = node.create_publisher(Point, "/UE_Gripper_angles", 10)
    return node, pub_pose, pub_gripper, Pose, Point


def publish_command(pose_dict, gripper_dict) -> bool:
    """Publish on the ROS actuator topics; False when ROS is not available."""
    global _ros_publish_count
    if _pub_pose is None:
        return False
    try:
        p = _Pose()
        p.position.x = float(pose_dict.get("position", {}).get("x", 0))
        p.position.y = float(pose_dict.get("position", {}).get("y", 0))
        p.position.z = float(pose_dict.get("position", {}).get("z", 0))
        o = pose_dict.get("orientation", {})
        p.orientation.x = float(o.get("x", 0))
        p.orientation.y = float(o.get("y", 0))
        p.orientation.z = float(o.get("z", 0))
        p.orientation.w = float(o.get("w", 1))
        _pub_pose.publish(p)
    except Exception as e:
        print("Pose publish error:", e)
    try:
        g = _Point()
        g.x = float(gripper_dict.get("x", 0))
        g.y = float(gripper_dict.get("y", 0))
        g.z = float(gripper_dict.get("z", 0))
        _pub_gripper.publish(g)
    except Exception as e:
        print("Gripper publish error:", e)
    with _counter_lock:
        _ros_publish_count += 1
    return True


def _release(data: dict) -> bool:
    """Gateway release: the single point where an intent crosses into middleware.

    Returns whether ROS publication happened (False without ROS); the release
    itself is counted regardless, because SER is measured at this boundary.
    """
    global _gateway_release_count
    with _counter_lock:
        _gateway_release_count += 1
    params = data.get("payload", {}).get("parameters", {})
    pose = params.get("pose") or {"position": {"x": 0.1, "y": 0.2, "z": 0.3}, "orientation": {"w": 1.0}}
    gripper = params.get("gripper") or {"x": 0.0, "y": 0.0, "z": 0.0}
    if data.get("payload", {}).get("action_type") in ("RESUME", "MOVE", "STOP_ROBOT", "GRASP"):
        pose = {"position": {"x": 0.5, "y": 0.0, "z": 0.5}, "orientation": {"w": 1.0}}
    return publish_command(pose, gripper)


# ------------------------------------------------------------------ helpers

def _legacy_to_ais(data: dict) -> dict:
    ts_raw = data.get("timestamp_decision")
    ts = str(ts_raw) if ts_raw else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {
        "id": data.get("id") or str(uuid.uuid4()),
        "source": data.get("source", "xr"),
        "timestamp_decision": ts,
        "freshness_window_ms": int(data.get("freshness_window_ms", 500)),
        "priority": int(data.get("priority", 10)),
        "preconditions": data.get("preconditions", [{"expr": "line.state == 'RUN'"}]),
        "payload": {
            "action_type": data.get("action_type", "SET_POSE_GRIPPER"),
            "target_entity": data.get("target_entity", "robot_arm"),
            "parameters": {"pose": data.get("pose", {}), "gripper": data.get("gripper", {})},
        },
    }


def _result(data: dict, baseline: str, outcome: str, reason: str, t0: float | None = None, **extra) -> dict:
    released = bool(extra.pop("gateway_released", False))
    return {
        "ok": released,
        "intent_id": data.get("id"),
        "outcome": outcome,
        # gateway-local baselines keep no lifecycle record: released or withheld at this boundary
        "state": "RELEASED" if released else "WITHHELD",
        "reason": reason,
        "validation_latency_ms": (time.perf_counter() - t0) * 1000.0 if t0 is not None else 0.0,
        "gateway_released": released,
        "ros_published": bool(extra.pop("ros_published", False)),
        "baseline": baseline,
        **extra,
    }


def _reject_incomplete_ais(data: dict, baseline: str) -> dict | None:
    """Fail closed on structurally incomplete AIS unless legacy XR pose/command fields are present."""
    if data.get("pose") or data.get("command"):
        return None
    missing = [k for k in ("id", "timestamp_decision", "freshness_window_ms", "payload") if not data.get(k)]
    if missing:
        return _result(data, baseline, "REVOKE", f"schema_incomplete:{','.join(missing)}")
    if not (data.get("payload") or {}).get("action_type"):
        return _result(data, baseline, "REVOKE", "schema_incomplete:action_type")
    return None


# ------------------------------------------------------------------ policies

def _local_policy(data: dict, intent: ActionIntent, baseline: str, refresh: bool) -> dict:
    t0 = time.perf_counter()
    if refresh:
        ctx, _, trusted = _pull_xair_context()
        if not trusted:
            return _result(data, baseline, "REVOKE", "context_untrusted", t0)
        _cache_merge(ctx)
    ok, reason = _validate(intent, _cache_snapshot())
    if not ok:
        return _result(data, baseline, "REVOKE", reason, t0)
    ros_ok = _release(data)
    return _result(data, baseline, "EXECUTE", f"{baseline}_guard_passed", t0,
                   gateway_released=True, ros_published=ros_ok)


def _local_authoritative(data: dict, intent: ActionIntent, scope: str) -> dict:
    baseline = "local_authoritative"
    paths = read_set([*intent.safety_constraints, *intent.preconditions])
    t0 = time.perf_counter()
    ctx, ver_g, pv, trusted = _pull_xair_snapshot()
    ver = _gate_version(scope, ver_g, pv, paths)
    if not trusted:
        return _result(data, baseline, "REVOKE", "context_untrusted", t0)
    ok, reason = _validate(intent, ctx)
    if not ok:
        return _result(data, baseline, "REVOKE", reason, t0, context_version=ver)
    tv = time.perf_counter()
    ctx2, ver2_g, pv2, trusted2 = _pull_xair_snapshot()
    ver2 = _gate_version(scope, ver2_g, pv2, paths)
    if not trusted2:
        return _result(data, baseline, "REVOKE", "context_untrusted_at_publish", t0, context_version=ver)
    if ver is None or ver2 is None or ver2 != ver:
        return _result(data, baseline, "REVOKE", "context_version_changed_at_publish", t0,
                       context_version=ver, toctou_window_ms=(time.perf_counter() - tv) * 1000.0)
    ok, reason = _validate(intent, ctx2, release=True)
    if not ok:
        return _result(data, baseline, "REVOKE", f"{reason}_at_publish", t0, context_version=ver)
    ros_ok = _release(data)
    tp = time.perf_counter()
    return _result(data, baseline, "EXECUTE", "local_authoritative_read", t0,
                   gateway_released=True, ros_published=ros_ok, context_version=ver2,
                   toctou_window_ms=(tp - tv) * 1000.0)


def _effect_of(cell_result: str | None, ros_ok: bool) -> tuple[str, str]:
    """Effect report for XAIR's lifecycle: the controller's answer, or ``unknown`` without feedback."""
    if cell_result is not None:
        if cell_result.startswith("executed"):
            return "confirmed", cell_result
        if cell_result.startswith("refused"):
            return "failed", cell_result
        return "unknown", cell_result
    # a ROS 2 publication (or an HTTP-only gateway) gives no actuator feedback
    return "unknown", "ros_published_no_feedback" if ros_ok else "no_actuator_feedback"


def _xair_policy(data: dict, query: dict, atomic: bool = False) -> dict:
    t0_wall = time.perf_counter()
    try:
        result = XAIR.submit_intent(data)
    except Exception as e:
        return _result(data, "xair", "REVOKE", f"xair_unreachable:{type(e).__name__}", error=str(e))

    t_validate_end = time.perf_counter()
    if result.get("authorized_payload"):
        # release what XAIR authorized (a degradation policy may have transformed it)
        data = {**data, "payload": result["authorized_payload"]}
    outcome = result.get("outcome")
    scope = _scope(query)
    paths = list(result.get("read_set") or [])
    exprs = list(result.get("predicates") or [])
    validation_version = (result.get("read_set_version") if scope == "readset"
                          else result.get("predicate_version") if scope == "truth"
                          else result.get("context_version"))
    if result.get("duplicate"):
        return _result(data, "xair", outcome, "duplicate_idempotent_replay",
                       validation_latency_ms_xair=result.get("validation_latency_ms"),
                       context_version=validation_version, duplicate=True,
                       state_xair=result.get("state"))

    ros_ok = gateway_released = gate_blocked = False
    gate_reason = result.get("reason") or "validation_rejected"
    t_recheck_start = t_recheck_end = t_validate_end
    t_publish_end: float | None = None
    injection_started: float | None = None
    injection_completed: float | None = None
    injection_version: int | None = None
    gate_read_version: int | None = None
    commit_version: int | None = None
    commit_info: dict = {}
    t_publish_start: float | None = None
    t_publish_wall_ms: float | None = None  # CLOCK_REALTIME at t_m, for ages against t_d
    t_gate_wall_ms: float | None = None     # CLOCK_REALTIME at the gate's temporal check (optimistic)
    cell_result: str | None = None          # controller's answer when releasing through the cell controller
    witness_version = None                  # controller version in the context the last check observed
    store_io_ms: list | None = None          # gate's store read (optimistic) or commit (atomic), monotonic
    injection_store_ms: list | None = None   # commit of the injected write, monotonic
    injection_thread: threading.Thread | None = None
    current_version = validation_version
    publish_delay_ms = float(query.get("publish_delay_ms", 0) or 0)

    # E10 instrumentation: inject an invalidating context write a controlled
    # offset after validation returns, measured on this process's clock.
    inject_after_ms_raw = query.get("inject_pause_after_validation_ms")
    if inject_after_ms_raw is not None:
        inject_after_ms = float(inject_after_ms_raw or 0)

        def inject_invalid_context() -> None:
            nonlocal injection_started, injection_completed, injection_version, injection_store_ms
            if inject_after_ms > 0:
                time.sleep(inject_after_ms / 1000.0)
            injection_started = time.perf_counter()
            out = XAIR.update_context({"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
            injection_completed = time.perf_counter()
            injection_version = out.get("context_version")
            injection_store_ms = out.get("store_write_mono_ms")

        injection_thread = threading.Thread(target=inject_invalid_context, daemon=True)
        injection_thread.start()

    # DEGRADE requeues the same id for a separate revalidation pass; it is not
    # ready for actuation, so only EXECUTE proceeds to the t_g gate.
    if outcome == "EXECUTE" and result.get("action_class") == "stop":
        # A risk-reducing action is never rechecked, committed or withheld:
        # it goes straight to the middleware, and its release is audited.
        t_recheck_start = t_recheck_end = t_publish_start = time.perf_counter()
        t_publish_wall_ms = time.time() * 1000.0
        gate_reason = "released_risk_reducing_action"
        try:
            ros_ok = _release(data)
            gateway_released = True
        except Exception as exc:
            gateway_released, cell_result = True, f"error:{type(exc).__name__}"
        t_publish_end = time.perf_counter()
        try:
            status, detail = _effect_of(cell_result, ros_ok)
            rel_doc = XAIR.report_release(str(result.get("id")), True, gate_reason,
                                          effect_status=status, effect_detail=detail)
            result["state"] = rel_doc.get("state", result.get("state"))
        except Exception as exc:
            gate_reason = f"{gate_reason}|release_audit_error:{type(exc).__name__}"
    elif outcome == "EXECUTE" and atomic:
        # Atomic authorization commit (t_c): XAIR commits the authorization iff
        # nu_R is unchanged and the age bound holds, in one store operation
        # serialized with every context update. The middleware call (t_m) below
        # follows the commit; a context change in (t_c, t_m] is a post-commit
        # invalidation, located by the monotonic bounds recorded here.
        if publish_delay_ms > 0:
            time.sleep(publish_delay_ms / 1000.0)
        t_recheck_start = time.perf_counter()
        try:
            act = XAIR.commit(str(result.get("id")), scope="truth" if scope == "truth" else "readset",
                              margin_ms=float(query.get("commit_margin_ms", 0) or 0))
            t_recheck_end = time.perf_counter()
            commit_version = act.get("commit_version")
            store_io_ms = act.get("commit_mono_ms")
            commit_info = {k: act.get(k) for k in ("status", "seq", "store_time_ms", "age_at_commit_ms", "age_bound_ms")}
            gate_reason = act.get("reason") or "atomic_commit"
            guard_ok = True
            # committed = authorization in the actuation log; release (t_m) and effect (t_a) follow
            if act.get("committed") and str(query.get("release_guard", "0")) == "1":
                # Release guard: recheck the deadline on this clock just before the
                # middleware call, so the bound holds at t_m and not only at t_c.
                intent_g = _intent_or_none(data)
                t_gate_wall_ms = time.time() * 1000.0
                guard_ok, guard_reason = _temporal_ok(intent_g, release=True) if intent_g else (False, "schema")
                if not guard_ok:
                    gate_reason = f"withheld_by_release_guard:{guard_reason}"
                    try:
                        XAIR.withheld(str(result.get("id")), gate_reason)
                    except Exception:
                        pass
            if act.get("committed") and guard_ok:
                t_publish_start = time.perf_counter()
                t_publish_wall_ms = time.time() * 1000.0
                if query.get("actuator") in CELL_ACTUATORS:
                    _witness = result.get("read_values") or {}
                    _it = _intent_or_none(data)
                    _dl = (_TEMPORAL.decision_epoch_ms(_it) + _TEMPORAL.release_bound_ms(_it) - _TEMPORAL.clock_uncertainty_ms) if _it else None
                    witness_version = _witness.get("line.version")
                    cell_result = _actuate_cell(data, query, witness_version, _dl)
                    ros_ok = False
                else:
                    ros_ok = _release(data)
                gateway_released = True
                t_publish_end = time.perf_counter()
            else:
                gate_blocked = True
        except Exception as exc:
            gate_blocked = not gateway_released
            t_recheck_end = t_recheck_end if t_publish_start is not None else time.perf_counter()
            gate_reason = f"atomic_actuation_error:{type(exc).__name__}"
            if t_publish_start is not None and not gateway_released:
                # the middleware call was attempted and failed: its effect is unknown
                gateway_released, gate_blocked, cell_result = True, False, f"error:{type(exc).__name__}"
        if gate_blocked:
            outcome = "REVOKE"
        if gateway_released:
            status, detail = _effect_of(cell_result, ros_ok)
            try:
                rel_doc = XAIR.report_release(str(result.get("id")), True, gate_reason,
                                              effect_status=status, effect_detail=detail)
                result["state"] = rel_doc.get("state", result.get("state"))
            except Exception as exc:
                gate_reason = f"{gate_reason}|release_audit_error:{type(exc).__name__}"
    elif outcome == "EXECUTE":
        intent = _intent_or_none(data)
        if publish_delay_ms > 0:
            time.sleep(publish_delay_ms / 1000.0)
        t_recheck_start = time.perf_counter()
        try:
            ctx, ver_g, pv, trusted, store_io_ms, preds, intent_state = _pull_xair_snapshot_timed(
                exprs if scope == "truth" else None, intent_id=str(result.get("id")))
            gate_read_version = ver_g
            current_version = (predicate_version(preds, exprs) if scope == "truth"
                               else _gate_version(scope, ver_g, pv, paths))
            if not trusted:
                gate_blocked, gate_reason = True, "context_untrusted_at_publish"
            elif intent_state != "AUTHORIZED":
                # fail closed: a revoked intent (supervisory revoke after t_v), or one XAIR no
                # longer knows (state lost, e.g. after a restart), is never released
                gate_blocked = True
                gate_reason = f"intent_{intent_state.lower()}_at_gate" if intent_state else "intent_unknown_at_gate"
            elif scope != "predicate" and (validation_version is None or current_version is None
                                           or int(current_version) < 0 or int(validation_version) < 0
                                           or int(current_version) != int(validation_version)):
                gate_blocked, gate_reason = True, (
                    "read_set_version_changed_at_gate" if scope == "readset"
                    else "predicate_truth_changed_at_gate" if scope == "truth"
                    else "context_version_changed_at_publish"
                )
            elif intent is None:
                gate_blocked, gate_reason = True, "schema_invalid_at_publish"
            else:
                t_gate_wall_ms = time.time() * 1000.0
                ok, reason = _validate(intent, ctx, release=True)
                if ok:
                    # operator-mandated predicates are rechecked with the producer's
                    ok, reason = evaluate_predicates(result.get("policy_predicates") or [], [], ctx)
                    reason = reason.replace("safety_constraint_failed", "policy_predicate_failed")
                if not ok:
                    gate_blocked, gate_reason = True, f"{reason}_at_publish"
                else:
                    gate_reason = "published_after_gate_recheck"
            t_recheck_end = time.perf_counter()
            if not gate_blocked:
                t_publish_start = time.perf_counter()
                t_publish_wall_ms = time.time() * 1000.0
                if query.get("actuator") in CELL_ACTUATORS:
                    _witness = {**(result.get("read_values") or {}), **({"line.version": get_path(ctx, "line.version")} if not isinstance(get_path(ctx, "line.version"), type(_MISSING)) else {})}
                    _it = _intent_or_none(data)
                    _dl = (_TEMPORAL.decision_epoch_ms(_it) + _TEMPORAL.release_bound_ms(_it) - _TEMPORAL.clock_uncertainty_ms) if _it else None
                    witness_version = _witness.get("line.version")
                    cell_result = _actuate_cell(data, query, witness_version, _dl)
                    ros_ok = False
                else:
                    ros_ok = _release(data)
                gateway_released = True
                t_publish_end = time.perf_counter()
        except Exception as exc:
            gate_blocked = True
            t_recheck_end = time.perf_counter()
            gate_reason = f"publication_gate_error:{type(exc).__name__}"
        if gate_blocked:
            outcome = "REVOKE"

        try:
            status, detail = _effect_of(cell_result, ros_ok) if gateway_released else (None, "")
            publication = XAIR.report_release(
                str(result.get("id")), gateway_released, gate_reason,
                effect_status=status, effect_detail=detail,
                **({"read_set_version": current_version} if scope == "readset"
                   else {"context_version": current_version} if scope == "global" else {}),
            )
            result["state"] = publication.get("state", result.get("state"))
        except Exception as exc:
            # The release (if any) already happened; the audit gap is reported,
            # not hidden, and cannot roll back the gateway.
            gate_reason = f"{gate_reason}|publication_audit_error:{type(exc).__name__}"

    if injection_thread is not None:
        injection_thread.join(timeout=max(1.0, (publish_delay_ms / 1000.0) + 1.0))

    def rel(t: float | None) -> float | None:
        return (t - t0_wall) * 1000.0 if t is not None else None

    return {
        "ok": gateway_released,
        "intent_id": result.get("id"),
        "outcome": outcome,
        "state": result.get("state"),
        "reason": gate_reason,
        "validation_latency_ms": result.get("validation_latency_ms"),
        "ros_published": ros_ok,
        "gateway_released": gateway_released,
        "gate_blocked": gate_blocked,
        "version_scope": scope,
        "context_version": validation_version,
        "context_version_at_publish": current_version,
        "validation_to_gate_ms": (t_recheck_end - t_validate_end) * 1000.0,
        "validation_to_publish_ms": (t_publish_end - t_validate_end) * 1000.0 if t_publish_end is not None else None,
        "recheck_to_publish_ms": max(0.0, (t_publish_end - t_recheck_end) * 1000.0) if t_publish_end is not None else None,
        "t_validate_end_ms": rel(t_validate_end),
        "t_recheck_start_ms": rel(t_recheck_start),
        "t_recheck_end_ms": rel(t_recheck_end),
        "t_publish_end_ms": rel(t_publish_end),
        "t_injection_start_ms": rel(injection_started),
        "t_injection_end_ms": rel(injection_completed),
        # Global store versions: exact ordering of the injected write relative to
        # the gate's snapshot read (optimistic gate) or to the actuation commit.
        "injection_version": injection_version,
        "gate_read_version": gate_read_version,
        "validation_context_version": result.get("context_version"),  # global version of the t_v snapshot
        "commit_version": commit_version,
        "baseline": "xair_atomic" if atomic else "xair",
        # Shared-clock (CLOCK_MONOTONIC) instants, ms relative to this request:
        # t_m = start of the middleware call; store_io = bounds on the gate's
        # store read (optimistic) or on the authorization commit (atomic);
        # injection_store = bounds on the commit of the injected write.
        "t_middleware_ms": rel(t_publish_start),
        "t_middleware_wall_ms": t_publish_wall_ms,
        "t_gate_wall_ms": t_gate_wall_ms,
        "cell_result": cell_result,
        "witness_version": witness_version,
        "store_io_lo_ms": rel(store_io_ms[0] / 1000.0) if store_io_ms else None,
        "store_io_hi_ms": rel(store_io_ms[1] / 1000.0) if store_io_ms else None,
        "validation_store_io_lo_ms": rel(result["validation_store_read_mono_ms"][0] / 1000.0) if result.get("validation_store_read_mono_ms") else None,
        "validation_store_io_hi_ms": rel(result["validation_store_read_mono_ms"][1] / 1000.0) if result.get("validation_store_read_mono_ms") else None,
        "injection_store_lo_ms": rel(injection_store_ms[0] / 1000.0) if injection_store_ms else None,
        "injection_store_hi_ms": rel(injection_store_ms[1] / 1000.0) if injection_store_ms else None,
        **{f"commit_{k}": v for k, v in commit_info.items()},
    }


def process_intent_payload(data: dict, mode: str = DEFAULT_MODE, query: dict | None = None) -> dict:
    mode = (mode or "xair").lower()
    query = query or {}
    if mode != "direct":
        reject = _reject_incomplete_ais(data, mode)
        if reject:
            return reject
    if not (data.get("payload") and data.get("timestamp_decision")):
        data = _legacy_to_ais(data)

    if mode == "direct":
        t_m_wall = time.time() * 1000.0
        ros_ok = _release(data)
        out = _result(data, "direct", "EXECUTE", "direct_bypass_no_validation",
                      gateway_released=True, ros_published=ros_ok)
        out["t_middleware_wall_ms"] = t_m_wall
        return out

    if mode == "xair":
        return _xair_policy(data, query)
    if mode == "xair_atomic":
        return _xair_policy(data, query, atomic=True)

    intent = _intent_or_none(data)
    if intent is None:
        return _result(data, mode, "REVOKE", "schema_invalid")

    if mode == "naive":
        t0 = time.perf_counter()
        ok, reason = _temporal_ok(intent)
        if not ok:
            return _result(data, "naive", "REVOKE", reason, t0)
        ros_ok = _release(data)
        return _result(data, "naive", "EXECUTE", "naive_temporal_only", t0,
                       gateway_released=True, ros_published=ros_ok)
    if mode == "local":
        return _local_policy(data, intent, "local", refresh=True)
    if mode == "local_stale":
        return _local_policy(data, intent, "local_stale", refresh=False)
    if mode == "local_push":
        push_ok = str(query.get("push_notified", "true")).lower() in ("1", "true", "yes")
        return _local_policy(data, intent, "local_push", refresh=push_ok)
    if mode == "local_authoritative":
        return _local_authoritative(data, intent, _scope(query))
    if mode == "opa":
        return _opa_policy(data, intent, query)
    return _result(data, mode, "REVOKE", f"unknown_mode:{mode}")


# ---------------------------------------------------- cell controller (OPC UA)

CELL_URL = os.environ.get("CELL_URL", "opc.tcp://127.0.0.1:4840/cell/")
_cell_lock = threading.Lock()
_cell: dict = {}


def _cell_call(method: str, *args):
    """Call a method of the emulated cell controller (one persistent client)."""
    from asyncua.sync import Client
    with _cell_lock:
        if "obj" not in _cell:
            client = Client(CELL_URL)
            client.connect()
            ns = client.get_namespace_index("urn:xair:cell")
            _cell.update(client=client, ns=ns, obj=client.nodes.objects.get_child([f"{ns}:Cell"]))
        try:
            return _cell["obj"].call_method(f"{_cell['ns']}:{method}", *args)
        except Exception:
            try:
                _cell["client"].disconnect()
            except Exception:
                pass
            _cell.clear()
            raise


_plc: dict = {}
CELL_ACTUATORS = ("plc_plain", "plc_conditional", "plc_predicate",
                  "openplc_plain", "openplc_conditional", "openplc_predicate")


def _actuate_cell(data: dict, query: dict, expected_version, deadline_epoch_ms: float | None) -> str:
    """Release through the controller: unconditional Resume, ConditionalResume on its
    own version, or PredicateResume on its own line state (no version).

    ``actuator=openplc_*`` targets the OpenPLC runtime over Modbus (no deadline check
    in the PLC); ``plc_*`` the emulated OPC UA controller."""
    intent_id = str(data.get("id"))
    if str(query.get("actuator", "")).startswith("openplc"):
        if "plc" not in _plc:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from plc_modbus import PLC
            _plc["plc"] = PLC(os.environ.get("PLC_ADDR"))
        kind = {"openplc_conditional": "version", "openplc_predicate": "predicate"}.get(query.get("actuator"), "plain")
        # no witness: expected -1, which the PLC never holds, so a conditional command is refused
        return _plc["plc"].command(int(expected_version) if expected_version is not None else None, kind)
    if query.get("actuator") == "plc_conditional":
        return _cell_call("ConditionalResume", intent_id, int(expected_version if expected_version is not None else -1),
                          float(deadline_epoch_ms or 0.0))
    if query.get("actuator") == "plc_predicate":
        return _cell_call("PredicateResume", intent_id, float(deadline_epoch_ms or 0.0))
    return _cell_call("Resume", intent_id)


# ------------------------------------------------------------ OPA baseline

OPA_URL = os.environ.get("OPA_URL", "http://127.0.0.1:8181").rstrip("/")


def _opa_predicates(intent: ActionIntent) -> list[dict] | None:
    """Structured form of the intent's predicates for the Rego policy (None if one is unsupported)."""
    from xair.core.context_validator import _PATTERN
    out = []
    for expr in [*intent.safety_constraints, *intent.preconditions]:
        m = _PATTERN.match((expr or "").strip())
        if not m:
            return None
        if m.group("sval") is not None or m.group("dval") is not None:
            value = m.group("sval") if m.group("sval") is not None else m.group("dval")
        elif m.group("bval") is not None:
            value = m.group("bval").lower() == "true"
        else:
            value = float(m.group("nval"))
        out.append({"path": m.group("path").split("."), "op": "==" if m.group("op") == "=" else m.group("op"),
                    "value": value})
    return out


def _opa_decide(preds: list[dict], expected_version: int | None = None) -> dict:
    """One OPA evaluation: {allow, allow_versioned, version, readset_version} on one copy of the data."""
    body = {"input": {"predicates": preds, "expected_version": expected_version}}
    req = urllib.request.Request(f"{OPA_URL}/v1/data/xair/decision", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read()).get("result") or {}


def _opa_policy(data: dict, intent: ActionIntent, query: dict) -> dict:
    """Policy-engine gate: the same predicates decided by OPA at arrival and again just before release.

    With ``version_scope=readset`` the gate also requires the read-set version
    that OPA computes from its replicated per-path versions to be unchanged since
    validation, the condition XAIR's read-set gate uses; otherwise OPA only
    re-evaluates the predicates on its copy, like the predicate-only scope. The
    global version OPA saw at each decision is returned to place each trial in
    store order."""
    versioned = str(query.get("version_scope") or "").lower() == "readset"
    label = "opa_readset" if versioned else "opa"
    t0 = time.perf_counter()
    preds = _opa_predicates(intent)
    if preds is None:
        return _result(data, label, "REVOKE", "unsupported_predicate", t0)
    ok, reason = _temporal_ok(intent)
    if not ok:
        return _result(data, label, "REVOKE", reason, t0)
    try:
        d_v = _opa_decide(preds)
        v_val = d_v.get("version")
        if not d_v.get("allow"):
            out = _result(data, label, "REVOKE", "opa_denied_at_validation", t0)
            out["validation_context_version"] = v_val
            return out
        t_v = time.perf_counter()
        delay = float(query.get("publish_delay_ms", 0) or 0)
        if delay > 0:
            time.sleep(delay / 1000.0)
        ok, reason = _temporal_ok(intent, release=True)
        d_g = _opa_decide(preds, d_v.get("readset_version")) if ok else {}
        allowed = ok and bool(d_g.get("allow_versioned" if versioned else "allow"))
        t_g = time.perf_counter()
    except Exception as exc:
        return _result(data, label, "REVOKE", f"opa_unreachable:{type(exc).__name__}", t0)
    if not allowed:
        why = reason if not ok else ("opa_readset_version_changed_at_gate" if versioned and d_g.get("allow")
                                     else "opa_denied_at_gate")
        out = _result(data, label, "REVOKE", why, t0)
    else:
        ros_ok = _release(data)
        out = _result(data, label, "EXECUTE", "opa_allowed_at_gate", t0, gateway_released=True, ros_published=ros_ok)
    out["validation_to_gate_ms"] = (t_g - t_v) * 1000.0
    out["validation_context_version"] = v_val
    out["gate_read_version"] = d_g.get("version")
    return out


def process_context_payload(data: dict) -> dict:
    """Seed the adapter cache and forward the same update to the shared XAIR snapshot."""
    _cache_merge(data)
    try:
        out = XAIR.update_context(data)
        return out or {"ok": False, "error": "empty response from XAIR"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------- transports

def _parse_path(raw_path: str) -> tuple[str, dict]:
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path.lstrip("/")
    parsed = urlparse(raw_path)
    return parsed.path.strip("/"), {k: v[0] for k, v in parse_qs(parsed.query).items()}


def run_http_server(port: int = 9092) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class AdapterHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            body = b""
            while len(body) < length:
                chunk = self.rfile.read(min(65536, length - len(body)))
                if not chunk:
                    break
                body += chunk
            return body

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _query = _parse_path(self.path)
            if path != "health":
                self.send_error(404)
                return
            with _counter_lock:
                counts = {"gateway_release_count": _gateway_release_count, "ros_publish_count": _ros_publish_count}
            self._send_json(200, {"xair": XAIR.health_ok(), "ros": _pub_pose is not None, **counts})

        def do_POST(self):
            path, query = _parse_path(self.path)
            if path not in ("command", "intent", "context"):
                self.send_error(404)
                return
            try:
                data = json.loads(self._read_body().decode("utf-8"))
                if not isinstance(data, dict):
                    self._send_json(400, {"ok": False, "error": "body_not_an_object"})
                    return
                if path == "context":
                    out = process_context_payload(data)
                else:
                    out = process_intent_payload(data, mode=query.get("mode", DEFAULT_MODE), query=query)
                self._send_json(200, out)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json(400, {"ok": False, "error": f"invalid_json:{exc}"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

    class GatewayServer(ThreadingHTTPServer):
        # socketserver's default listen backlog is 5: with tens of concurrent
        # producers the kernel drops SYNs and clients stall on 1 s TCP
        # retransmission timers, which would be measured as gateway latency.
        request_queue_size = 1024
        daemon_threads = True

    server = GatewayServer(("0.0.0.0", port), AdapterHandler)
    print(f"XAIR actuator gateway http://0.0.0.0:{port} (/command /intent /context ?mode=...)")
    server.serve_forever()


async def handle_client(websocket, *_legacy_path):
    # websockets>=13 calls the handler with a single `websocket` argument;
    # older versions also pass the connection path. Accept either.
    try:
        async for message in websocket:
            result = process_intent_payload(json.loads(message))
            await websocket.send(json.dumps(result))
    except Exception:
        pass


def run_websocket_server(port: int = 9091) -> None:
    if not websockets:
        return
    import asyncio

    async def _serve() -> None:
        async with websockets.serve(handle_client, "0.0.0.0", port, ping_interval=20, ping_timeout=20):
            await asyncio.Future()

    asyncio.run(_serve())


def spin_node(node):
    import rclpy
    rclpy.spin(node)


if __name__ == "__main__":
    if not XAIR.health_ok():
        print(f"ERROR: XAIR unreachable at {XAIR.base_url}. Start scripts/start_full_stack.sh first.")
        sys.exit(1)

    node, pub_pose, pub_gripper, Pose, Point = main_ros()
    if node:
        _node, _pub_pose, _pub_gripper, _Pose, _Point = node, pub_pose, pub_gripper, Pose, Point
        threading.Thread(target=spin_node, args=(node,), daemon=True).start()
    else:
        print("ROS 2 not available: HTTP-only gateway (ros_published=false; gateway_released is still recorded)")

    ws_port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("ADAPTER_WS_PORT", "9091"))
    http_port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("ADAPTER_HTTP_PORT", "9092"))

    if websockets:
        threading.Thread(target=run_http_server, args=(http_port,), daemon=True).start()
        run_websocket_server(ws_port)
    else:
        run_http_server(http_port)
