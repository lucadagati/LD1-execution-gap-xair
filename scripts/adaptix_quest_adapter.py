#!/usr/bin/env python3
"""
Adapter WebSocket + HTTP → XAIR (HTTP API) → ROS 2 for AdaptiX / Unity Editor.
Supports: POST /command (XR), POST /intent (AIS), POST /context (line state).
Query param ?mode=xair|direct|naive|local|local_stale|local_push|local_authoritative on /intent.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))
if (_SCRIPTS.parent / "xair").is_dir():
    sys.path.insert(0, str(_SCRIPTS.parent))
elif (_SCRIPTS.parent / "XAIR_Runtime" / "xair").is_dir():
    sys.path.insert(0, str(_SCRIPTS.parent / "XAIR_Runtime"))

from xair_http_client import XAIRHttpClient
from xair.core.deep_merge import deep_merge

try:
    import websockets
except ImportError:
    websockets = None

XAIR = XAIRHttpClient()
DEFAULT_MODE = os.environ.get("XAIR_VALIDATION_MODE", "xair")
_context_cache: dict = {}
_PREDICATE = re.compile(
    r"^(\w+(?:\.\w+)*)\s*(==|!=|<=|>=|<|>)\s*('([^']*)'|\"([^\"]*)\"|(-?\d+(?:\.\d+)?))$"
)


def _resolve_context(path: str):
    node = _context_cache
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _eval_preconditions(data: dict) -> tuple[bool, str]:
    for item in data.get("preconditions", []):
        expr = item.get("expr", item) if isinstance(item, dict) else str(item)
        expr = expr.strip()
        if not expr:
            continue
        m = _PREDICATE.match(expr)
        if not m:
            return False, f"unsupported:{expr}"
        path, op, _, sval, dval, nval = m.groups()
        left = _resolve_context(path)
        if sval is not None:
            right = sval
        elif dval is not None:
            right = dval
        else:
            right = float(nval) if "." in (nval or "") else int(nval)
        ops = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b,
               "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
               ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
        if left is None or not ops[op](left, right):
            return False, f"precondition_failed:{expr}"
    return True, "pre_ok"


def setup_ros():
    try:
        import rclpy
        from geometry_msgs.msg import Pose, Point
        return rclpy, Pose, Point
    except ImportError:
        return None, None, None


def main_ros():
    rclpy, Pose, Point = setup_ros()
    if rclpy is None:
        return None, None, None, None, None
    rclpy.init()
    node = rclpy.create_node("adaptix_quest_adapter")
    pub_pose = node.create_publisher(Pose, "/UE_TCP_position", 10)
    pub_gripper = node.create_publisher(Point, "/UE_Gripper_angles", 10)
    return node, pub_pose, pub_gripper, Pose, Point


_node = _pub_pose = _pub_gripper = _Pose = _Point = None
_ros_publish_count = 0


def publish_command(pose_dict, gripper_dict):
    global _pub_pose, _pub_gripper, _Pose, _Point, _ros_publish_count
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
    _ros_publish_count += 1
    return True


def _legacy_to_ais(data: dict) -> dict:
    ts_raw = data.get("timestamp_decision")
    if ts_raw:
        ts = str(ts_raw)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    pose = data.get("pose", {})
    gripper = data.get("gripper", {})
    return {
        "source": data.get("source", "xr"),
        "timestamp_decision": ts,
        "freshness_window_ms": int(data.get("freshness_window_ms", 500)),
        "priority": int(data.get("priority", 10)),
        "preconditions": data.get("preconditions", [{"expr": "line.state == 'RUN'"}]),
        "payload": {
            "action_type": data.get("action_type", "SET_POSE_GRIPPER"),
            "target_entity": data.get("target_entity", "robot_arm"),
            "parameters": {"pose": pose, "gripper": gripper},
        },
    }


def _parse_freshness_ok(data: dict) -> tuple[bool, str]:
    ts_raw = data.get("timestamp_decision")
    window = int(data.get("freshness_window_ms", 500))
    if not ts_raw:
        return True, "no_timestamp"
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except ValueError:
        return False, "invalid_timestamp"
    now = datetime.now(timezone.utc)
    delta_ms = (now - ts).total_seconds() * 1000.0
    if delta_ms > window:
        return False, f"freshness_exceeded:{delta_ms:.1f}ms"
    return True, "fresh"


def _publish_from_intent(data: dict) -> bool:
    params = data.get("payload", {}).get("parameters", {})
    pose = params.get("pose") or {"position": {"x": 0.1, "y": 0.2, "z": 0.3}, "orientation": {"w": 1.0}}
    gripper = params.get("gripper") or {"x": 0.0, "y": 0.0, "z": 0.0}
    if data.get("payload", {}).get("action_type") in ("RESUME", "MOVE", "STOP_ROBOT", "GRASP"):
        pose = {"position": {"x": 0.5, "y": 0.0, "z": 0.5}, "orientation": {"w": 1.0}}
    return publish_command(pose, gripper)


def _pull_xair_context() -> tuple[dict, int | None, bool]:
    try:
        snap = XAIR.get_context()
        ctx = snap.get("context") or {}
        ver = snap.get("context_version")
        trusted = snap.get("context_trusted", True)
        return ctx, int(ver) if ver is not None else None, bool(trusted)
    except Exception:
        return {}, None, False


def _sync_cache_from_xair() -> None:
    global _context_cache
    ctx, _, _ = _pull_xair_context()
    if ctx:
        _context_cache = deep_merge(_context_cache, ctx)


def _local_validate(data: dict, t0: float, baseline: str) -> dict | None:
    fresh_ok, fresh_reason = _parse_freshness_ok(data)
    if not fresh_ok:
        return {
            "ok": False,
            "intent_id": data.get("id"),
            "outcome": "REVOKE",
            "state": "REVOKED",
            "reason": fresh_reason,
            "validation_latency_ms": (time.perf_counter() - t0) * 1000,
            "ros_published": False,
            "baseline": baseline,
        }
    pre_ok, pre_reason = _eval_preconditions(data)
    if not pre_ok:
        return {
            "ok": False,
            "intent_id": data.get("id"),
            "outcome": "REVOKE",
            "state": "REVOKED",
            "reason": pre_reason,
            "validation_latency_ms": (time.perf_counter() - t0) * 1000,
            "ros_published": False,
            "baseline": baseline,
        }
    return None


def _reject_incomplete_ais(data: dict, baseline: str) -> dict | None:
    """Fail closed on structurally incomplete AIS unless legacy XR pose/command fields are present."""
    if data.get("pose") or data.get("command"):
        return None
    missing = [k for k in ("timestamp_decision", "freshness_window_ms", "payload") if not data.get(k)]
    if missing:
        return {
            "ok": False,
            "intent_id": data.get("id"),
            "outcome": "REVOKE",
            "state": "REVOKED",
            "reason": f"schema_incomplete:{','.join(missing)}",
            "validation_latency_ms": 0.0,
            "ros_published": False,
            "baseline": baseline,
        }
    if not (data.get("payload") or {}).get("action_type"):
        return {
            "ok": False,
            "intent_id": data.get("id"),
            "outcome": "REVOKE",
            "state": "REVOKED",
            "reason": "schema_incomplete:action_type",
            "validation_latency_ms": 0.0,
            "ros_published": False,
            "baseline": baseline,
        }
    return None


def process_intent_payload(data: dict, mode: str = DEFAULT_MODE, query: dict | None = None) -> dict:
    mode = (mode or "xair").lower()
    if mode != "direct":
        reject = _reject_incomplete_ais(data, mode)
        if reject:
            return reject
    if not (data.get("payload") and data.get("timestamp_decision")):
        data = _legacy_to_ais(data)

    if mode == "direct":
        ros_ok = _publish_from_intent(data)
        return {
            "ok": True,
            "intent_id": data.get("id"),
            "outcome": "EXECUTE",
            "state": "EXECUTED",
            "reason": "direct_bypass_no_validation",
            "validation_latency_ms": 0.0,
            "ros_published": ros_ok,
            "baseline": "direct",
        }

    if mode == "naive":
        fresh_ok, fresh_reason = _parse_freshness_ok(data)
        if not fresh_ok:
            return {
                "ok": False,
                "intent_id": data.get("id"),
                "outcome": "REVOKE",
                "state": "REVOKED",
                "reason": fresh_reason,
                "validation_latency_ms": 0.0,
                "ros_published": False,
                "baseline": "naive",
            }
        ros_ok = _publish_from_intent(data)
        return {
            "ok": True,
            "intent_id": data.get("id"),
            "outcome": "EXECUTE",
            "state": "EXECUTED",
            "reason": "naive_temporal_only",
            "validation_latency_ms": 0.0,
            "ros_published": ros_ok,
            "baseline": "naive",
        }

    if mode == "local":
        t0 = time.perf_counter()
        _sync_cache_from_xair()
        fail = _local_validate(data, t0, "local")
        if fail:
            return fail
        ros_ok = _publish_from_intent(data)
        return {
            "ok": True,
            "intent_id": data.get("id"),
            "outcome": "EXECUTE",
            "state": "EXECUTED",
            "reason": "local_guard_temporal_context",
            "validation_latency_ms": (time.perf_counter() - t0) * 1000,
            "ros_published": ros_ok,
            "baseline": "local",
        }

    if mode == "local_stale":
        t0 = time.perf_counter()
        fail = _local_validate(data, t0, "local_stale")
        if fail:
            return fail
        ros_ok = _publish_from_intent(data)
        return {
            "ok": True,
            "intent_id": data.get("id"),
            "outcome": "EXECUTE",
            "state": "EXECUTED",
            "reason": "local_stale_cache",
            "validation_latency_ms": (time.perf_counter() - t0) * 1000,
            "ros_published": ros_ok,
            "baseline": "local_stale",
        }

    query = query or {}

    if mode == "local_push":
        t0 = time.perf_counter()
        push_ok = str(query.get("push_notified", "true")).lower() in ("1", "true", "yes")
        if push_ok:
            _sync_cache_from_xair()
        fail = _local_validate(data, t0, "local_push")
        if fail:
            return fail
        ros_ok = _publish_from_intent(data)
        return {
            "ok": True,
            "intent_id": data.get("id"),
            "outcome": "EXECUTE",
            "state": "EXECUTED",
            "reason": "local_push_invalidate",
            "validation_latency_ms": (time.perf_counter() - t0) * 1000,
            "ros_published": ros_ok,
            "baseline": "local_push",
        }

    if mode == "local_authoritative":
        t0 = time.perf_counter()
        ctx, ver, trusted = _pull_xair_context()
        if not trusted:
            return {
                "ok": False,
                "intent_id": data.get("id"),
                "outcome": "REVOKE",
                "state": "REVOKED",
                "reason": "context_untrusted",
                "validation_latency_ms": (time.perf_counter() - t0) * 1000,
                "ros_published": False,
                "baseline": "local_authoritative",
            }
        global _context_cache
        _context_cache = deep_merge(_context_cache, ctx)
        fail = _local_validate(data, t0, "local_authoritative")
        if fail:
            return fail
        tv = time.perf_counter()
        ctx2, ver2, trusted2 = _pull_xair_context()
        if not trusted2 or (ver is not None and ver2 is not None and int(ver2) != int(ver)):
            return {
                "ok": False,
                "intent_id": data.get("id"),
                "outcome": "REVOKE",
                "state": "REVOKED",
                "reason": "context_version_changed_at_publish",
                "validation_latency_ms": (time.perf_counter() - t0) * 1000,
                "ros_published": False,
                "context_version": ver,
                "toctou_window_ms": (time.perf_counter() - tv) * 1000,
                "baseline": "local_authoritative",
            }
        _context_cache = deep_merge(_context_cache, ctx2)
        if not _eval_preconditions(data)[0]:
            return {
                "ok": False,
                "intent_id": data.get("id"),
                "outcome": "REVOKE",
                "state": "REVOKED",
                "reason": "precondition_failed_at_publish",
                "validation_latency_ms": (time.perf_counter() - t0) * 1000,
                "ros_published": False,
                "baseline": "local_authoritative",
            }
        ros_ok = _publish_from_intent(data)
        tp = time.perf_counter()
        return {
            "ok": True,
            "intent_id": data.get("id"),
            "outcome": "EXECUTE",
            "state": "EXECUTED",
            "reason": "local_authoritative_read",
            "validation_latency_ms": (tp - t0) * 1000,
            "ros_published": ros_ok,
            "context_version": ver2,
            "toctou_window_ms": (tp - tv) * 1000,
            "baseline": "local_authoritative",
        }

    try:
        t0_wall = time.perf_counter()
        result = XAIR.submit_intent(data)
    except Exception as e:
        return {"ok": False, "error": str(e), "ros_published": False, "baseline": "xair"}

    t_validate_end = time.perf_counter()
    outcome = result.get("outcome")
    validation_version = result.get("context_version")
    ros_ok = False
    toctou_blocked = False
    t_recheck_start = t_recheck_end = t_publish_end = t_validate_end
    publish_delay_ms = float(query.get("publish_delay_ms", 0) or 0)
    if outcome in ("EXECUTE", "DEGRADE"):
        if publish_delay_ms > 0:
            time.sleep(publish_delay_ms / 1000.0)
        t_recheck_start = time.perf_counter()
        try:
            snap = XAIR.get_context()
            current_version = snap.get("context_version")
            if (
                validation_version is not None
                and current_version is not None
                and int(current_version) != int(validation_version)
            ):
                toctou_blocked = True
                outcome = "REVOKE"
            elif snap.get("context_trusted") is False:
                toctou_blocked = True
                outcome = "REVOKE"
            else:
                t_recheck_end = time.perf_counter()
                ros_ok = _publish_from_intent(data)
                t_publish_end = time.perf_counter()
        except Exception:
            ros_ok = False
            outcome = "REVOKE"
            t_recheck_end = t_publish_end = time.perf_counter()
        if toctou_blocked:
            t_recheck_end = time.perf_counter()
    toctou_window_ms = (t_recheck_start - t_validate_end) * 1000.0
    recheck_to_publish_ms = max(0.0, (t_publish_end - t_recheck_end) * 1000.0)
    return {
        "ok": outcome in ("EXECUTE", "DEGRADE") and not toctou_blocked,
        "intent_id": result.get("id"),
        "outcome": outcome,
        "state": result.get("state"),
        "reason": "context_version_changed_at_publish" if toctou_blocked else result.get("reason"),
        "validation_latency_ms": result.get("validation_latency_ms"),
        "ros_published": ros_ok,
        "context_version": validation_version,
        "toctou_window_ms": toctou_window_ms,
        "recheck_to_publish_ms": recheck_to_publish_ms,
        "t_validate_end_ms": (t_validate_end - t0_wall) * 1000.0,
        "t_recheck_start_ms": (t_recheck_start - t0_wall) * 1000.0,
        "t_recheck_end_ms": (t_recheck_end - t0_wall) * 1000.0,
        "t_publish_end_ms": (t_publish_end - t0_wall) * 1000.0,
        "baseline": "xair",
    }


def process_context_payload(data: dict) -> dict:
    global _context_cache
    _context_cache = deep_merge(_context_cache, data)
    try:
        out = XAIR.update_context(data)
        if not out:
            return {"ok": False, "error": "empty response from XAIR"}
        return out
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _parse_path(raw_path: str) -> tuple[str, dict]:
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path.lstrip("/")
    parsed = urlparse(raw_path)
    path = parsed.path.strip("/")
    qs = parse_qs(parsed.query)
    flat = {k: v[0] for k, v in qs.items()}
    return path, flat


def run_http_server(port=9092):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class AdapterHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return b""
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
            if path == "health":
                ok = XAIR.health_ok()
                self._send_json(200, {"xair": ok, "ros_publish_count": _ros_publish_count})
            else:
                self.send_error(404)

        def do_POST(self):
            path, query = _parse_path(self.path)
            if path not in ("command", "intent", "context"):
                self.send_error(404)
                return
            try:
                data = json.loads(self._read_body().decode("utf-8"))
                if path == "context":
                    out = process_context_payload(data)
                else:
                    mode = query.get("mode", DEFAULT_MODE)
                    out = process_intent_payload(data, mode=mode, query=query)
                self._send_json(200, out)
            except json.JSONDecodeError as exc:
                self._send_json(400, {"ok": False, "error": f"invalid_json:{exc}"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

    server = ThreadingHTTPServer(("0.0.0.0", port), AdapterHandler)
    print(f"AdaptiX+XAIR HTTP http://0.0.0.0:{port} (/command /intent /context ?mode=...)")
    server.serve_forever()


async def handle_client(websocket, path):
    try:
        async for message in websocket:
            data = json.loads(message)
            result = process_intent_payload(data)
            await websocket.send(json.dumps(result))
    except Exception:
        pass


def run_websocket_server(port=9091):
    if not websockets:
        return
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    start = websockets.serve(handle_client, "0.0.0.0", port, ping_interval=20, ping_timeout=20)
    loop.run_until_complete(start)
    loop.run_forever()


def spin_node(node):
    import rclpy
    rclpy.spin(node)


if __name__ == "__main__":
    import threading

    if not XAIR.health_ok():
        print("ERRORE: XAIR non raggiungibile. Avvia prima start_full_stack.sh o uvicorn su :8080")
        sys.exit(1)

    node, pub_pose, pub_gripper, Pose, Point = main_ros()
    if node:
        globals()["_node"] = node
        globals()["_pub_pose"] = pub_pose
        globals()["_pub_gripper"] = pub_gripper
        globals()["_Pose"] = Pose
        globals()["_Point"] = Point
        threading.Thread(target=spin_node, args=(node,), daemon=True).start()
    else:
        print("ROS 2 non disponibile: adapter HTTP-only (ros_published=false unless ROS up)")

    ws_port = int(sys.argv[1]) if len(sys.argv) > 1 else 9091
    http_port = int(sys.argv[2]) if len(sys.argv) > 2 else 9092

    enable_ws = os.environ.get("XAIR_ADAPTER_WEBSOCKET", "").lower() in ("1", "true", "yes")
    if websockets and enable_ws:
        threading.Thread(target=run_http_server, args=(http_port,), daemon=True).start()
        run_websocket_server(ws_port)
    else:
        if enable_ws and not websockets:
            print("[WARN] websockets package missing; HTTP-only adapter")
        run_http_server(http_port)
