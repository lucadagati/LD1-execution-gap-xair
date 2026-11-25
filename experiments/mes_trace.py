"""Replay of a public production event log (MES-like) as discrete machine states.

Dataset: "Production Analysis with Process Mining Technology" (4TU.ResearchData,
DOI 10.4121/uuid:68726926-5ac5-4fab-b873-ee76ea412399): 4543 work steps of a
job shop over 89 days, each with a resource, a start and a completion time,
and a report type (S setup, D production, B breakdown, as the dataset's
documentation analyses them). Timestamps have a resolution of one minute. A resource is IDLE when no
step is active on it, SETUP while a setup step is active, and BUSY otherwise.
The archive is downloaded once into a cache directory and verified by SHA-256.
"""

from __future__ import annotations

import bisect
import csv
import datetime as dt
import hashlib
import io
import os
import re
import urllib.request
import zipfile
from pathlib import Path

from common import ROOT

URL = "https://data.4tu.nl/file/67d71073-7ece-4455-9840-9d48ee480160/68a909ed-ea0c-4e30-841a-4bce336e8e8c"
SHA256 = "b3a43f457393bc9dd0c616db189d01aa55e823f202cc8ce0cd537572840644cf"
CACHE = Path(os.environ.get("XAIR_DATA_CACHE", ROOT / "experiments" / ".cache")) / "production_log.zip"


def archive() -> Path:
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".part")
        with urllib.request.urlopen(URL, timeout=120) as r, tmp.open("wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.rename(CACHE)
    digest = hashlib.sha256(CACHE.read_bytes()).hexdigest()
    if digest != SHA256:
        raise RuntimeError(f"{CACHE}: SHA-256 {digest} does not match the published archive")
    return CACHE


def key(resource: str) -> str:
    return re.sub(r"\W+", "_", resource.strip().lower()).strip("_")


def _t(s: str) -> float:
    return dt.datetime.strptime(s, "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc).timestamp()


class Timeline:
    """Per-machine state as a step function of trace time (seconds since the first event)."""

    def __init__(self) -> None:
        with zipfile.ZipFile(archive()) as z, z.open("Production_Data.csv") as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="latin-1")))
        steps = [(key(r["Resource"]), _t(r["Start Timestamp"]), _t(r["Complete Timestamp"]), r["Report Type"]) for r in rows]
        self.t0 = min(a for _, a, _, _ in steps)
        # per-machine steps in start order: (start, end, report type, part description)
        self.steps: dict[str, list[tuple[float, float, str, str]]] = {}
        for r, (m, a, b, kind) in zip(rows, steps):
            self.steps.setdefault(m, []).append((a - self.t0, b - self.t0, kind, r.get("Part Desc.", "").strip()))
        for v in self.steps.values():
            v.sort()
        self.span = max(b for _, _, b, _ in steps) - self.t0
        points: dict[str, list[tuple[float, int, int]]] = {}
        for m, a, b, kind in steps:
            points.setdefault(m, []).extend([(a - self.t0, 1, int(kind == "S")), (b - self.t0, -1, -int(kind == "S"))])
        self.machines = sorted(points)
        self.changes: dict[str, tuple[list[float], list[str]]] = {}
        for m, pts in points.items():
            active = setup = 0
            times, states, last = [0.0], ["IDLE"], "IDLE"
            pts = sorted(pts)
            for j, (t, d_active, d_setup) in enumerate(pts):
                active += d_active
                setup += d_setup
                if j + 1 < len(pts) and pts[j + 1][0] == t:
                    continue    # apply every step boundary at t first: no zero-length IDLE between adjacent steps
                s = "IDLE" if active <= 0 else "SETUP" if setup > 0 else "BUSY"
                if s != last:
                    times.append(t)
                    states.append(s)
                    last = s
            self.changes[m] = (times, states)

    def state(self, m: str, t: float) -> str:
        times, states = self.changes[m]
        return states[bisect.bisect_right(times, t) - 1]

    def next_change(self, m: str, t: float) -> float:
        times, _ = self.changes[m]
        i = bisect.bisect_right(times, t)
        return times[i] if i < len(times) else float("inf")

    def events(self, t_from: float, t_to: float) -> list[tuple[float, str, str]]:
        """[(trace time, machine, new state)] in [t_from, t_to), in time order."""
        out = []
        for m, (times, states) in self.changes.items():
            lo = bisect.bisect_left(times, t_from)
            hi = bisect.bisect_left(times, t_to)
            out.extend((times[i], m, states[i]) for i in range(max(lo, 1), hi))
        return sorted(out)
