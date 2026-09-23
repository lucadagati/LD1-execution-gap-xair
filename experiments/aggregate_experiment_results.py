#!/usr/bin/env python3
"""Aggregate every suite into the summary that backs the paper's numbers.

Rates carry Wilson 95% intervals; percentiles are nearest-rank (see common.py).
Suites that need extra infrastructure (E6 netem, E8-Gazebo, E15 OPC UA) are
read from ``<results>/host-a-2026-09-10/`` when present, and labelled as such.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from common import RESULTS_DIR, percentile, truthy, wilson_ci

LEGACY_DIR = "host-a-2026-09-10"  # optional archived campaign, not part of the paper


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def rate(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "rate": k / n if n else 0.0, "ci95": [round(lo, 4), round(hi, 4)]}


def lat(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean": statistics.fmean(values) if values else 0.0,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values, default=0.0),
    }


def fnum(row: dict, key: str) -> float | None:
    v = row.get(key)
    return float(v) if v not in (None, "", "None") else None


def e1(rows: list[dict]) -> dict:
    out = {}
    for b in sorted({r["baseline"] for r in rows}):
        sub = [r for r in rows if r["baseline"] == b]
        out[b] = {
            "SER": rate(sum(int(r["stale_executed"]) for r in sub), len(sub)),
            "CRR": rate(sum(int(r["correct_revoke"]) for r in sub), len(sub)),
            "validation_latency_ms": lat([float(r["validation_latency_ms"]) for r in sub if float(r["validation_latency_ms"]) > 0]),
            "e2e_latency_ms": lat([float(r["e2e_latency_ms"]) for r in sub]),
            "max_intent_age_at_submit_ms": max(float(r["intent_age_at_submit_ms"]) for r in sub),
        }
        reasons: dict[str, int] = defaultdict(int)
        for r in sub:
            reasons[r.get("reason") or ""] += 1
        out[b]["reasons"] = dict(reasons)
    return out


def fpr(rows: list[dict]) -> dict:
    return {b: rate(sum(int(r["wrongful_revoke"]) for r in rows if r["baseline"] == b),
                    sum(1 for r in rows if r["baseline"] == b))
            for b in sorted({r["baseline"] for r in rows})}


def e10(rows: list[dict]) -> dict:
    inj = [r for r in rows if r["inject"] == "1"]
    ctrl = [r for r in rows if r["inject"] == "0"]
    cells = {}
    for off in sorted({float(r["inject_offset_ms"]) for r in inj}):
        cell = [r for r in inj if float(r["inject_offset_ms"]) == off]
        windows: dict[str, int] = defaultdict(int)
        for r in cell:
            windows[r["injection_window"]] += 1
        cells[f"{off:g}"] = {
            "injected": len(cell),
            "windows": dict(windows),
            "blocked": sum(int(r["gate_blocked"]) for r in cell),
            "released": sum(int(r["gateway_released"]) for r in cell),
            "stale": rate(sum(int(r["stale_publish"]) for r in cell), len(cell)),
            "potential_stale": sum(int(r["potential_stale_publish"]) for r in cell),
            "post_release_invalidation": sum(int(r["post_release_invalidation"]) for r in cell),
        }
    before = [r for r in inj if r["injection_window"] == "before_recheck"]
    return {
        "runs": len(rows),
        "injected": len(inj),
        "controls": len(ctrl),
        "controls_released": sum(int(r["gateway_released"]) for r in ctrl),
        "by_offset_ms": cells,
        "stale_given_before_recheck": rate(sum(int(r["stale_publish"]) for r in before), len(before)),
        "validation_to_gate_ms_injected": lat([float(r["validation_to_gate_ms"]) for r in inj]),
        "validation_to_release_ms_controls": lat([v for r in ctrl if (v := fnum(r, "validation_to_publish_ms")) is not None]),
        "residual_recheck_to_release_ms": lat([v for r in rows if (v := fnum(r, "recheck_to_publish_ms")) is not None]),
    }


def e11(paths: list[Path]) -> dict:
    pooled: list[dict] = []
    per_seed = {}
    for p in paths:
        rows = load(p)
        if not rows:
            continue
        pooled += rows
        drifted = [r for r in rows if r["drifted"] == "1"]
        per_seed[rows[0]["seed"]] = {
            "runs": len(rows), "drifted": len(drifted),
            "correct": rate(sum(int(r["correct"]) for r in rows), len(rows)),
            "stale_on_drifted": rate(sum(int(r["stale_executed"]) for r in drifted), len(drifted)),
            "wrongful_on_valid": rate(sum(int(r["wrongful_revoke"]) for r in rows if r["drifted"] == "0"),
                                      sum(1 for r in rows if r["drifted"] == "0")),
        }
    drifted = [r for r in pooled if r["drifted"] == "1"]
    valid = [r for r in pooled if r["drifted"] == "0"]
    cost = {}
    for k in sorted({int(r["predicate_count"]) for r in valid}):
        cost[str(k)] = lat([float(r["validation_latency_ms"]) for r in valid if int(r["predicate_count"]) == k])
    return {
        "per_seed": per_seed,
        "pooled": {
            "runs": len(pooled), "drifted": len(drifted),
            "correct": rate(sum(int(r["correct"]) for r in pooled), len(pooled)),
            "stale_on_drifted": rate(sum(int(r["stale_executed"]) for r in drifted), len(drifted)),
            "wrongful_on_valid": rate(sum(int(r["wrongful_revoke"]) for r in valid), len(valid)),
        },
        "validation_cost_by_predicate_count_valid_only": cost,
    }


def e12(rows: list[dict]) -> dict:
    cells: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        cells[f"{r['mode']}|{r['producers']}|{r['context_kb']}"].append(r)
    out = {}
    for key, sub in sorted(cells.items()):
        out[key] = {
            "repetitions": len(sub),
            "released": sum(int(r["released"]) for r in sub),
            "trials": sum(int(r["trials"]) for r in sub),
            "connection_retries": sum(int(r.get("connection_retries") or 0) for r in sub),
            "median_throughput_ips": statistics.median(float(r["throughput_ips"]) for r in sub),
            "median_p50_ms": statistics.median(float(r["e2e_p50_ms"]) for r in sub),
            "median_p99_ms": statistics.median(float(r["e2e_p99_ms"]) for r in sub),
            "max_p99_ms": max(float(r["e2e_p99_ms"]) for r in sub),
        }
    return out


def grouped_rate(rows: list[dict], keys: tuple[str, ...], field: str) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups["|".join(r[k] for k in keys)].append(r)
    return {k: rate(sum(int(truthy(r[field])) for r in v), len(v)) for k, v in sorted(groups.items())}


def e6(rows: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[f"d{r['delay_ms']}_j{r['jitter_ms']}_l{r['loss_pct']}"].append(r)
    out = {}
    for key, v in groups.items():
        drifted = [r for r in v if r["drifted"] == "1"]
        valid = [r for r in v if r["drifted"] == "0"]
        reasons: dict[str, int] = defaultdict(int)
        for r in drifted:
            if r["gateway_released"] == "0":
                reason = r.get("reason") or ""
                kind = "context" if "precondition" in reason else "temporal" if ("obsolete" in reason or "deadline" in reason) else reason
                reasons[kind] += 1
        out[key] = {
            "netem_applied": all(r["netem_applied"] == "1" for r in v),
            "stale_on_drifted": rate(sum(int(r["gateway_released"]) for r in drifted), len(drifted)),
            "revoke_reasons_drifted": dict(reasons),
            "wrongful_on_valid": rate(sum(1 - int(r["gateway_released"]) for r in valid), len(valid)),
            "e2e_ms": lat([float(r["e2e_latency_ms"]) for r in v]),
        }
    return out


def e8(paths: list[Path]) -> dict:
    out = {}
    for p in paths:
        rows = load(p)
        if not rows:
            continue
        camp = {}
        for b in sorted({r["baseline"] for r in rows}):
            sub = [r for r in rows if r["baseline"] == b]
            camp[b] = {
                "released": rate(sum(int(r["gateway_released"]) for r in sub), len(sub)),
                "ros_observed": sum(r["ros_observed"] == "1" for r in sub),
                "ros_witness_agrees": rate(sum(r["ros_witness_agrees"] == "1" for r in sub), len(sub)),
                "sim_motion": rate(sum(int(r["sim_motion"]) for r in sub), len(sub)),
                "max_intent_age_at_submit_ms": max(float(r["intent_age_at_submit_ms"]) for r in sub),
            }
        out[rows[0]["campaign"]] = camp
    return out


def legacy(base: Path) -> dict:
    out: dict = {"host": LEGACY_DIR}
    e6 = load(base / "e6_network.csv")
    if e6:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in e6:
            groups[f"d{r['delay_ms']}_j{r['jitter_ms']}_l{r['loss_pct']}"].append(r)
        out["e6_network"] = {
            k: {"revoke": rate(sum(r["outcome"] == "REVOKE" for r in v), len(v)),
                "median_e2e_ms": statistics.median(float(r["e2e_latency_ms"]) for r in v),
                "reason_logged": "reason" in v[0], "valid_controls": sum(1 for r in v if r.get("drifted") == "0")}
            for k, v in groups.items()
        }
    e8 = load(base / "e8_gazebo_cell_sim.csv")
    if e8:
        out["e8_gazebo_campaign2"] = {
            b: {"released": rate(sum(truthy(r["stale_executed"]) for r in e8 if r["baseline"] == b), sum(1 for r in e8 if r["baseline"] == b)),
                "ros_witness_agrees": rate(sum(truthy(r["witness_agreement"]) for r in e8 if r["baseline"] == b), sum(1 for r in e8 if r["baseline"] == b)),
                "sim_motion": rate(sum(truthy(r["sim_motion"]) for r in e8 if r["baseline"] == b), sum(1 for r in e8 if r["baseline"] == b))}
            for b in sorted({r["baseline"] for r in e8})
        }
    pooled = base / "e8_gazebo_pooled_summary.json"
    if pooled.exists():
        out["e8_gazebo_pooled_summary"] = json.loads(pooled.read_text())
    e15 = load(base / "e15_opcua_hil.csv")
    if e15:
        out["e15_opcua"] = grouped_rate(e15, ("mode",), "stale_executed")
        out["e15_transport"] = sorted({r["transport"] for r in e15})
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    R = args.results
    out_path = args.out or R / "paper_metrics_summary.json"

    summary: dict = {"results_dir": str(R)}
    if (R / "e0_lifecycle.json").exists():
        e0 = json.loads((R / "e0_lifecycle.json").read_text())
        summary["e0"] = {"passed": e0["passed"], "total": e0["total"]}
    if rows := load(R / "e1_baselines.csv"):
        summary["e1"] = e1(rows)
    if rows := load(R / "e1_fpr.csv"):
        summary["e1_fpr"] = fpr(rows)
    if rows := load(R / "e3_conflict_http.csv"):
        summary["e3"] = {"runs": len(rows), "xr_wins": sum(int(r["xr_wins"]) for r in rows),
                         "conflict_violations": sum(int(r["cv"]) for r in rows)}
    if rows := load(R / "e4_load_http.csv"):
        summary["e4"] = {k: float(v) for k, v in rows[0].items()}
    if rows := load(R / "e9_consistency_sweep.csv"):
        summary["e9"] = {"by_policy": grouped_rate(rows, ("policy",), "stale_executed"),
                         "by_policy_delay": grouped_rate(rows, ("policy", "delay_ms"), "stale_executed")}
    if rows := load(R / "e10_toctou.csv"):
        summary["e10"] = e10(rows)
    if rows := load(R / "e10_toctou_boundary.csv"):
        summary["e10_boundary"] = e10(rows)
    e11_paths = sorted(R.glob("e11_stratified_seed*.csv"))
    if e11_paths:
        summary["e11"] = e11(e11_paths)
    if rows := load(R / "e12_scaling.csv"):
        summary["e12"] = e12(rows)
    if rows := load(R / "e13_faults.csv"):
        summary["e13"] = {"by_fault": grouped_rate(rows, ("fault",), "pass"),
                          "passed": sum(truthy(r["pass"]) for r in rows), "total": len(rows)}
    if rows := load(R / "e14_variants.csv"):
        summary["e14"] = grouped_rate(rows, ("scenario", "baseline"), "stale_executed")
    e6_runs = {p.stem: e6(load(p)) for p in sorted(R.glob("e6_network*.csv")) if load(p)}
    if e6_runs:
        summary["e6"] = e6_runs
    e8_paths = sorted(R.glob("e8_gazebo_campaign*.csv"))
    if e8_paths:
        summary["e8_gazebo"] = e8(e8_paths)
    if rows := load(R / "e15_opcua_hil.csv"):
        summary["e15"] = {"by_mode": grouped_rate(rows, ("mode",), "stale_executed"),
                          "transport": sorted({r["transport"] for r in rows})}
    if (R / LEGACY_DIR).is_dir():
        summary["legacy_host_a"] = legacy(R / LEGACY_DIR)

    out_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
