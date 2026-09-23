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
  local_authoritative  read the shared snapshot, validate, recheck version at t_p
  xair                 central validation at t_v + adapter recheck at t_p

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
from xair_http_client import XAIRHttpClient  # noqa: E402

try:
    import websockets
except ImportError:
    websockets = None

XAIR = XAIRHttpClient()
DEFAULT_MODE = os.environ.get("XAIR_VALIDATION_MODE", "xair")
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


def _temporal_ok(intent: ActionIntent) -> tuple[bool, str]:
    return _TEMPORAL.validate(intent)


def _predicates_ok(intent: ActionIntent, context: dict) -> tuple[bool, str]:
    return evaluate_predicates(intent.safety_constraints, intent.preconditions, context)


def _validate(intent: ActionIntent, context: dict) -> tuple[bool, str]:
    ok, reason = _temporal_ok(intent)
    if not ok:
        return ok, reason
    return _predicates_ok(intent, context)


def _pull_xair_context() -> tuple[dict, int | None, bool]:
    try:
        snap = XAIR.get_context()
        ver = snap.get("context_version")
        return (snap.get("context") or {}), (int(ver) if ver is not None else None), bool(snap.get("context_trusted", False))
    except Exception:
        return {}, None, False


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
    node = rclpy.create_node("adaptix_quest_adapter")
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
        "state": "EXECUTED" if released else "REVOKED",
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


def _local_authoritative(data: dict, intent: ActionIntent) -> dict:
    baseline = "local_authoritative"
    t0 = time.perf_counter()
    ctx, ver, trusted = _pull_xair_context()
    if not trusted:
        return _result(data, baseline, "REVOKE", "context_untrusted", t0)
    ok, reason = _validate(intent, ctx)
    if not ok:
        return _result(data, baseline, "REVOKE", reason, t0, context_version=ver)
    tv = time.perf_counter()
    ctx2, ver2, trusted2 = _pull_xair_context()
    if not trusted2:
        return _result(data, baseline, "REVOKE", "context_untrusted_at_publish", t0, context_version=ver)
    if ver is None or ver2 is None or ver2 != ver:
        return _result(data, baseline, "REVOKE", "context_version_changed_at_publish", t0,
                       context_version=ver, toctou_window_ms=(time.perf_counter() - tv) * 1000.0)
    ok, reason = _validate(intent, ctx2)
    if not ok:
        return _result(data, baseline, "REVOKE", f"{reason}_at_publish", t0, context_version=ver)
    ros_ok = _release(data)
    tp = time.perf_counter()
    return _result(data, baseline, "EXECUTE", "local_authoritative_read", t0,
                   gateway_released=True, ros_published=ros_ok, context_version=ver2,
                   toctou_window_ms=(tp - tv) * 1000.0)


def _xair_policy(data: dict, query: dict) -> dict:
    t0_wall = time.perf_counter()
    try:
        result = XAIR.submit_intent(data)
    except Exception as e:
        return _result(data, "xair", "REVOKE", f"xair_unreachable:{type(e).__name__}", error=str(e))

    t_validate_end = time.perf_counter()
    outcome = result.get("outcome")
    validation_version = result.get("context_version")
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
    injection_thread: threading.Thread | None = None
    current_version = validation_version
    publish_delay_ms = float(query.get("publish_delay_ms", 0) or 0)

    # E10 instrumentation: inject an invalidating context write a controlled
    # offset after validation returns, measured on this process's clock.
    inject_after_ms_raw = query.get("inject_pause_after_validation_ms")
    if inject_after_ms_raw is not None:
        inject_after_ms = float(inject_after_ms_raw or 0)

        def inject_invalid_context() -> None:
            nonlocal injection_started, injection_completed
            if inject_after_ms > 0:
                time.sleep(inject_after_ms / 1000.0)
            injection_started = time.perf_counter()
            XAIR.update_context({"line": {"state": "PAUSED"}, "gripper": {"state": "CLOSED"}})
            injection_completed = time.perf_counter()

        injection_thread = threading.Thread(target=inject_invalid_context, daemon=True)
        injection_thread.start()

    # DEGRADE requeues the same id for a separate revalidation pass; it is not
    # ready for actuation, so only EXECUTE proceeds to the t_p gate.
    if outcome == "EXECUTE":
        intent = _intent_or_none(data)
        if publish_delay_ms > 0:
            time.sleep(publish_delay_ms / 1000.0)
        t_recheck_start = time.perf_counter()
        try:
            ctx, current_version, trusted = _pull_xair_context()
            if not trusted:
                gate_blocked, gate_reason = True, "context_untrusted_at_publish"
            elif validation_version is None or current_version is None or int(current_version) != int(validation_version):
                gate_blocked, gate_reason = True, "context_version_changed_at_publish"
            elif intent is None:
                gate_blocked, gate_reason = True, "schema_invalid_at_publish"
            else:
                ok, reason = _validate(intent, ctx)
                if not ok:
                    gate_blocked, gate_reason = True, f"{reason}_at_publish"
                else:
                    gate_reason = "published_after_gate_recheck"
            t_recheck_end = time.perf_counter()
            if not gate_blocked:
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
            publication = XAIR.report_publication(
                str(result.get("id")), published=gateway_released, reason=gate_reason,
                context_version=current_version,
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
        "baseline": "xair",
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
        ros_ok = _release(data)
        return _result(data, "direct", "EXECUTE", "direct_bypass_no_validation",
                       gateway_released=True, ros_published=ros_ok)

    if mode == "xair":
        return _xair_policy(data, query)

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
        return _local_authoritative(data, intent)
    return _result(data, mode, "REVOKE", f"unknown_mode:{mode}")


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
    print(f"AdaptiX+XAIR gateway http://0.0.0.0:{port} (/command /intent /context ?mode=...)")
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
