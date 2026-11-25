"""Shared helpers for the experiment suites.

Endpoints and the output directory are configurable so a campaign can run
next to other services on the same host:

  XAIR_URL          XAIR core API            (default http://127.0.0.1:8080)
  ADAPTER_URL       actuator gateway          (default http://127.0.0.1:9092)
  XAIR_RESULTS_DIR  where CSV/JSON are written (default experiments/results)

Every rate reported in the paper is computed with ``wilson_ci`` and every
latency percentile with ``percentile`` (nearest rank), both defined here.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xair.core.runtime import percentile  # noqa: E402,F401  (single definition)

XAIR = os.environ.get("XAIR_URL", "http://127.0.0.1:8080").rstrip("/")
ADAPTER = os.environ.get("ADAPTER_URL", "http://127.0.0.1:9092").rstrip("/")
RESULTS_DIR = Path(os.environ.get("XAIR_RESULTS_DIR", ROOT / "experiments" / "results"))
AUDIT_FILE = Path(os.environ.get("ROS_AUDIT_FILE", RESULTS_DIR / "ros_audit_state.json"))


def now_iso(offset_ms: float = 0.0) -> str:
    ts = datetime.now(timezone.utc).timestamp() + offset_ms / 1000.0
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def http_json(url: str, body: dict | list | None = None, method: str | None = None, timeout: float = 20.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def adapter(path: str, body: dict, timeout: float = 20.0, **query) -> dict:
    """POST to the gateway; ``query`` becomes the query string (e.g. mode=xair)."""
    qs = urlencode({k: str(v) for k, v in query.items() if v is not None})
    t0 = time.perf_counter()
    out = http_json(f"{ADAPTER}/{path}" + (f"?{qs}" if qs else ""), body, timeout=timeout)
    out["e2e_latency_ms"] = (time.perf_counter() - t0) * 1000.0
    return out


def xair_context(body: dict | None = None) -> dict:
    """POST (remote writer) or GET the shared XAIR snapshot."""
    return http_json(f"{XAIR}/v1/context/snapshot", body)


def wait_xair_state(state: str, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if (xair_context().get("context") or {}).get("line", {}).get("state") == state:
            return True
        time.sleep(0.01)
    return False


def audit_count() -> int | None:
    """Pose-message count of the independent ROS witness (None if it is not running)."""
    if not AUDIT_FILE.exists():
        return None
    for _ in range(3):
        try:
            return int(json.loads(AUDIT_FILE.read_text()).get("pose_count", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            time.sleep(0.02)
    return None


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def released(resp: dict) -> bool:
    """Primary endpoint: did the intent cross the gateway boundary?"""
    return bool(resp.get("gateway_released"))


def write_csv(path: Path, rows: list[dict]) -> Path:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


URLError = urllib.error.URLError
