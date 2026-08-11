#!/usr/bin/env python3
"""
Aggregate Unity TestResults JSON into CSV/plots for IEEE TII paper.
Reads: AdaptiX-Quest/TestResults/*.json (exported by TestResultsExporter)
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IN = ROOT.parent / "AdaptiX-Quest" / "TestResults"


def parse_outcome(entry: dict) -> tuple[str, float]:
    resp = entry.get("response", {})
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except json.JSONDecodeError:
            return "UNKNOWN", 0.0
    if not isinstance(resp, dict):
        return "UNKNOWN", 0.0
    outcome = resp.get("outcome", "UNKNOWN")
    lat = float(resp.get("validation_latency_ms", 0) or 0)
    return outcome, lat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_IN)
    parser.add_argument("--out", type=Path, default=ROOT / "experiments" / "results" / "unity_e1_aggregated.csv")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(args.input.glob("*.json"))
    if not files:
        print(f"No JSON in {args.input}; run Unity ManufacturingTestOrchestrator first.")
        return 1

    rows = []
    for f in files:
        data = json.loads(f.read_text())
        outcome, lat = parse_outcome(data)
        baseline = data.get("baseline", "xair")
        revoked = 1 if outcome == "REVOKE" else 0
        executed = 1 if outcome == "EXECUTE" else 0
        ser = executed if baseline != "xair" and data.get("scenario") == "e1" else (0 if outcome == "REVOKE" else executed)
        rows.append({
            "file": f.name,
            "scenario": data.get("scenario"),
            "run": data.get("run"),
            "baseline": baseline,
            "outcome": outcome,
            "executed": executed,
            "revoked": revoked,
            "validation_latency_ms": lat,
        })

    with args.out.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"files": len(files), "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
