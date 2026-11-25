"""Replay of a public industrial telemetry dataset as context updates.

Dataset: "Condition monitoring of hydraulic systems" (Helwig, Pignanelli,
Schuetze, I2MTC 2015; UCI Machine Learning Repository, DOI 10.24432/C5CW21,
CC BY 4.0). A hydraulic test rig repeats 60 s load cycles; 17 sensors are
sampled at 100 Hz (pressures PS1-6, motor power EPS1), 10 Hz (flows FS1-2),
or 1 Hz (temperatures, vibration, efficiency, cooling). Per cycle, five
condition variables (cooler, valve, pump leakage, accumulator, stable flag)
change only at cycle boundaries.

``notifications`` turns the recording into the stream an OPC UA
subscription with a given publishing interval would deliver: at every
interval, each tag whose sampled value changed since the previous
notification (absolute deadband 0), in one context update. The archive is
downloaded once into a cache directory and verified by SHA-256.
"""

from __future__ import annotations

import hashlib
import io
import os
import urllib.request
import zipfile
from pathlib import Path

from common import ROOT

URL = "https://archive.ics.uci.edu/static/public/447/condition+monitoring+of+hydraulic+systems.zip"
SHA256 = "24128aad2ee45eea7e6b63ebbd9992cdf25d0483a2cebefbfc13bc69079af1f2"
CACHE = Path(os.environ.get("XAIR_DATA_CACHE", ROOT / "experiments" / ".cache")) / "uci_hydraulic.zip"
RATES_HZ = {**{f"PS{i}": 100 for i in range(1, 7)}, "EPS1": 100, "FS1": 10, "FS2": 10,
            **{f"TS{i}": 1 for i in range(1, 5)}, "VS1": 1, "CE": 1, "CP": 1, "SE": 1}
PROFILE = ("cooler_pct", "valve_pct", "pump_leakage", "accumulator_bar", "stable_flag")
CYCLE_S = 60


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


def load_cycles(first: int, count: int) -> tuple[dict[str, list[list[float]]], list[dict[str, float]]]:
    """Return ({sensor: [cycle samples]}, [cycle profile]) for cycles [first, first+count)."""
    sensors: dict[str, list[list[float]]] = {}
    with zipfile.ZipFile(archive()) as z:
        for name in RATES_HZ:
            with z.open(f"{name}.txt") as f:
                lines = io.TextIOWrapper(f).readlines()[first:first + count]
            sensors[name] = [[float(v) for v in line.split()] for line in lines]
        with z.open("profile.txt") as f:
            rows = io.TextIOWrapper(f).readlines()[first:first + count]
    profile = [dict(zip(PROFILE, (float(v) for v in row.split()))) for row in rows]
    return sensors, profile


def notifications(sensors: dict[str, list[list[float]]], profile: list[dict[str, float]],
                  publishing_interval_ms: float) -> list[tuple[float, dict]]:
    """[(t_seconds, context_patch)] for an OPC UA-style subscription at the given interval."""
    n_cycles = len(profile)
    step = publishing_interval_ms / 1000.0
    last: dict[str, float] = {}
    out: list[tuple[float, dict]] = []
    t = 0.0
    while t < n_cycles * CYCLE_S:
        cycle, within = int(t // CYCLE_S), t % CYCLE_S
        tele = {}
        for name, rate in RATES_HZ.items():
            samples = sensors[name][cycle]
            value = samples[min(int(within * rate), len(samples) - 1)]
            if last.get(name) != value:
                tele[name] = value
                last[name] = value
        patch: dict = {"telemetry": tele} if tele else {}
        if within < step:  # cycle boundary: condition variables (MES-like discrete state)
            patch["rig"] = dict(profile[cycle])
        if patch:
            out.append((t, patch))
        t = round(t + step, 6)
    return out
