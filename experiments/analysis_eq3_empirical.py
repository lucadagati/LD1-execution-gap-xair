"""Eq. (3) on the embedded boards: median-based vs empirical-distribution prediction.

Reads embedded/<board>/e16_context_churn.csv (global scope, related_same pattern) and
compares the measured revocation rate with the prediction evaluated (a) on the median
validation_to_gate_ms and (b) averaged over the per-trial values, for both forms.
Also reads the larger campaign in results/embedded_eq3. Writes results/eq3_empirical.json.
"""
import json
import numpy as np
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data" / "execution-gap" / "embedded"
out = {}
for board in ("rpi4", "vf2"):
    d = pd.read_csv(ROOT / board / "e16_context_churn.csv")
    g = d[(d.version_scope == "global") & (d.pattern == "related_same") & (d.target_rate_hz > 0)]
    for rate, x in g.groupby("target_rate_hz"):
        dt = x.validation_to_gate_ms.to_numpy() / 1e3
        n = len(x)
        meas = 1 - float(x.gateway_released.mean())
        se = (meas * (1 - meas) / n) ** 0.5
        out[f"{board}@{int(rate)}Hz"] = {
            "n": n, "measured": meas, "binomial_se": se,
            "median_dt_ms": float(np.median(dt) * 1e3),
            "p95_dt_ms": float(np.quantile(dt, .95) * 1e3),
            "poisson_median": float(1 - np.exp(-rate * np.median(dt))),
            "periodic_median": float(min(1, rate * np.median(dt))),
            "poisson_empirical": float((1 - np.exp(-rate * dt)).mean()),
            "periodic_empirical": float(np.minimum(1, rate * dt).mean()),
        }
# Larger campaign (scripts/run_embedded_eq3.sh): 3 seeds x 400 trials per cell, both patterns.
import glob
NEW = Path(__file__).resolve().parent / "results" / "embedded_eq3"
for board in ("rpi4", "vf2"):
    files = sorted(glob.glob(str(NEW / board / "e16_eq3_seed*.csv")))
    if not files:
        continue
    d = pd.concat([pd.read_csv(f) for f in files])
    for (pattern, rate), x in d.groupby(["pattern", "target_rate_hz"]):
        dt = x.validation_to_gate_ms.to_numpy() / 1e3
        n = len(x)
        meas = 1 - float(x.gateway_released.mean())
        out[f"eq3run:{board}:{pattern}@{int(rate)}Hz"] = {
            "n": n, "measured": meas, "ci95": 1.96 * (meas * (1 - meas) / n) ** 0.5,
            "median_dt_ms": float(np.median(dt) * 1e3),
            "p95_dt_ms": float(np.quantile(dt, .95) * 1e3),
            "periodic_median": float(min(1, rate * np.median(dt))),
            "periodic_empirical": float(np.minimum(1, rate * dt).mean()),
            "poisson_empirical": float((1 - np.exp(-rate * dt)).mean()),
        }
p = Path(__file__).resolve().parent / "results" / "eq3_empirical.json"
p.write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
