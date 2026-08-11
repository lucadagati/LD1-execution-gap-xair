#!/usr/bin/env python3
"""Parametric sensitivity sweep for E1b (freshness, pause-ms)."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()

    rows = []
    script = ROOT / "experiments" / "run_e1_baselines.py"
    for freshness in (200, 500, 1000):
        for pause in (200, 400):
            out = RESULTS / f"sensitivity_f{freshness}_p{pause}.csv"
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--runs", str(args.runs),
                    "--freshness-ms", str(freshness),
                    "--pause-ms", str(pause),
                    "--baselines", "xair", "direct",
                    "--out-csv", str(out),
                ],
                check=True,
            )
            with out.open() as f:
                for row in csv.DictReader(f):
                    if row["baseline"] == "xair":
                        rows.append({
                            "freshness_ms": freshness,
                            "pause_ms": pause,
                            "SER": float(row.get("stale_executed") or 0),
                            "outcome": row.get("outcome"),
                        })

    sens_out = RESULTS / "sensitivity_sweep.csv"
    with sens_out.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {sens_out}")


if __name__ == "__main__":
    main()
