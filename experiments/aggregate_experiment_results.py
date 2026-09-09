#!/usr/bin/env python3
"""
Aggregate experiment CSV/JSON into summary metrics for IEEE paper.
Computes SER, POA, FPR, conflict violations, VL with Wilson 95% CI where applicable.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results"
PAPER_FIGURES = ROOT.parent / "ResearchTrack" / "execution-gap-paper" / "figures"


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return 0.0, 0.0, 0.0
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n) / denom
    return p, max(0, center - margin), min(1, center + margin)


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def summarize_e1(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    out = {}
    for baseline in sorted(set(r["baseline"] for r in rows)):
        sub = [r for r in rows if r["baseline"] == baseline]
        n = len(sub)
        unknown = sum(1 for r in sub if r.get("outcome") == "UNKNOWN")
        stale = sum(1 for r in sub if str(r.get("stale_executed", "0")) in ("1", "True", "true"))
        obsolete = sum(1 for r in sub if str(r.get("obsolete_intent", "0")) in ("1", "True", "true"))
        correct = sum(1 for r in sub if str(r.get("correct_revoke", "0")) in ("1", "True", "true"))
        lats = [float(r["validation_latency_ms"]) for r in sub if float(r.get("validation_latency_ms") or 0) > 0]
        ser_p, ser_lo, ser_hi = wilson_ci(stale, n)
        poa_denom = max(obsolete - unknown, 1) if obsolete else n
        poa_correct = min(correct, poa_denom)
        poa_p, poa_lo, poa_hi = wilson_ci(poa_correct, poa_denom)
        out[baseline] = {
            "attempted": n,
            "unknown_rate": unknown / n,
            "SER": ser_p,
            "SER_ci95": [ser_lo, ser_hi],
            "POA": poa_p if obsolete else 1.0,
            "POA_ci95": [poa_lo, poa_hi],
            "vl_p99_ms": sorted(lats)[int(len(lats) * 0.99) - 1] if lats else 0,
            "vl_max_ms": max(lats) if lats else 0,
            "vl_mean_ms": sum(lats) / len(lats) if lats else 0,
        }
    return out


def summarize_fpr(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    n = len(rows)
    wrong = sum(1 for r in rows if str(r.get("wrongful_revoke", "0")) in ("1", "True", "true"))
    p, lo, hi = wilson_ci(wrong, n)
    return {"FPR": p, "FPR_ci95": [lo, hi], "runs": n}


def summarize_e3(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    cv = sum(int(r.get("cv", 0)) for r in rows)
    xr_wins = sum(int(r.get("xr_wins", 0)) for r in rows)
    return {
        "conflict_violations": cv,
        "xr_win_rate": xr_wins / len(rows),
        "runs": len(rows),
    }


def summarize_e4(path: Path) -> dict:
    rows = load_csv(path)
    return rows[0] if rows else {}


def summarize_e8(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    out = {}
    for baseline in sorted(set(r["baseline"] for r in rows)):
        sub = [r for r in rows if r["baseline"] == baseline]
        n = len(sub)
        unknown = sum(1 for r in sub if str(r.get("unknown", "0")) in ("1", "True", "true"))
        stale = sum(1 for r in sub if str(r.get("stale_executed", "0")) in ("1", "True", "true"))
        observed = sum(1 for r in sub if str(r.get("stale_observed", "0")) in ("1", "True", "true"))
        agree = sum(1 for r in sub if str(r.get("witness_agreement", "True")) in ("1", "True", "true"))
        has_witness = any(r.get("ros_observed") not in (None, "", "None") for r in sub)
        sim = sum(1 for r in sub if str(r.get("sim_motion", "0")) in ("1", "True", "true"))
        arms = [float(r.get("arm_delta") or 0) for r in sub if float(r.get("arm_delta") or 0) > 0]
        known = n - unknown
        ser_all_p, ser_all_lo, ser_all_hi = wilson_ci(stale, n)
        ser_known_p, ser_known_lo, ser_known_hi = wilson_ci(stale, known) if known else (0.0, 0.0, 0.0)
        obs_p, obs_lo, obs_hi = wilson_ci(observed, known) if known else (0.0, 0.0, 0.0)
        out[baseline] = {
            "runs": n,
            "known_runs": known,
            "unknown_rate": unknown / n if n else 0,
            "stale_executed_rate": ser_known_p,
            "stale_executed_ci95": [ser_known_lo, ser_known_hi],
            "stale_observed_rate": obs_p if has_witness else None,
            "stale_observed_ci95": [obs_lo, obs_hi] if has_witness else None,
            "witness_agreement_rate": agree / n if (n and has_witness) else None,
            "stale_executed_rate_all_runs": ser_all_p,
            "stale_executed_ci95_all_runs": [ser_all_lo, ser_all_hi],
            "sim_motion_rate": sim / n if n else 0,
            "arm_delta_mean": sum(arms) / len(arms) if arms else 0.0,
        }
    return out


def summarize_e9(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    n = len(rows)
    local_stale = sum(1 for r in rows if str(r.get("local_stale", "0")) in ("1", "True", "true"))
    xair_blocked = sum(1 for r in rows if str(r.get("xair_blocked", "0")) in ("1", "True", "true"))
    obs_rows = [r for r in rows if r.get("local_ros_observed") not in (None, "", "None")]
    local_obs = sum(1 for r in obs_rows if str(r.get("local_ros_observed", "0")) in ("1", "True", "true"))
    xair_obs = sum(1 for r in obs_rows if str(r.get("xair_ros_observed", "0")) in ("1", "True", "true"))
    ls_p, ls_lo, ls_hi = wilson_ci(local_stale, n)
    xb_p, xb_lo, xb_hi = wilson_ci(xair_blocked, n)
    out = {
        "runs": n,
        "local_stale_rate": ls_p,
        "local_stale_ci95": [ls_lo, ls_hi],
        "xair_blocked_rate": xb_p,
        "xair_blocked_ci95": [xb_lo, xb_hi],
    }
    if obs_rows:
        lo_p, lo_lo, lo_hi = wilson_ci(local_obs, len(obs_rows))
        xo_p, xo_lo, xo_hi = wilson_ci(xair_obs, len(obs_rows))
        out["witnessed_runs"] = len(obs_rows)
        out["local_stale_observed_rate"] = lo_p
        out["local_stale_observed_ci95"] = [lo_lo, lo_hi]
        out["xair_publish_observed_rate"] = xo_p
        out["xair_publish_observed_ci95"] = [xo_lo, xo_hi]
    return out


def summarize_e6(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    by_cfg: dict[str, list[dict]] = {}
    for r in rows:
        key = f"d{r.get('delay_ms',0)}_j{r.get('jitter_ms',0)}_l{r.get('loss_pct',0)}"
        by_cfg.setdefault(key, []).append(r)
    conditions = {}
    for key, sub in sorted(by_cfg.items()):
        n = len(sub)
        rev = sum(1 for r in sub if r.get("outcome") == "REVOKE")
        unk = sum(1 for r in sub if r.get("outcome") in (None, "", "UNKNOWN"))
        p, lo, hi = wilson_ci(rev, n)
        conditions[key] = {
            "runs": n,
            "revoke_rate": p,
            "revoke_ci95": [lo, hi],
            "unknown_rate": unk / n if n else 0,
        }
    return {"conditions": conditions, "rows": len(rows)}


def summarize_e9_sweep(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    out: dict[str, dict] = {}
    for policy in sorted(set(r["policy"] for r in rows)):
        sub = [r for r in rows if r["policy"] == policy]
        stale = sum(int(r.get("stale_executed", 0)) for r in sub)
        p, lo, hi = wilson_ci(stale, len(sub))
        out[policy] = {"runs": len(sub), "stale_rate": p, "stale_ci95": [lo, hi]}
    return out


def summarize_e10(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    windows = [float(r.get("toctou_window_ms") or 0) for r in rows]
    injected = [r for r in rows if int(r.get("inject", 0))]
    blocked = sum(int(r.get("toctou_blocked", 0)) for r in injected)
    stale = sum(int(r.get("stale_publish", 0)) for r in injected)
    n_inj = len(injected)
    ws = sorted(windows)
    _, lo, hi = wilson_ci(blocked, n_inj) if n_inj else (0.0, 0.0, 0.0)
    return {
        "runs": len(rows),
        "injected_runs": n_inj,
        "toctou_blocked": blocked,
        "toctou_blocked_rate": blocked / n_inj if n_inj else 0,
        "blocked_ci95": [lo, hi],
        "stale_publish": stale,
        "stale_publish_rate": stale / n_inj if n_inj else 0,
        "window_p50_ms": ws[len(ws) // 2] if ws else 0,
        "window_p99_ms": ws[int(len(ws) * 0.99) - 1] if ws else 0,
    }


def summarize_e11(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    drifted = [r for r in rows if int(r.get("drifted", 0) or 0) == 1]
    correct = sum(int(r.get("correct", 0) or 0) for r in rows)
    stale = sum(int(r.get("stale_executed", 0) or 0) for r in drifted)
    p, lo, hi = wilson_ci(stale, len(drifted)) if drifted else (0.0, 0.0, 0.0)
    cp, clo, chi = wilson_ci(correct, len(rows))
    return {
        "runs": len(rows),
        "drifted_runs": len(drifted),
        "correct_rate": cp,
        "correct_ci95": [clo, chi],
        "rate": p,
        "ci95": [lo, hi],
    }


def summarize_generic_rate(path: Path, field: str) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    stale = sum(int(r.get(field, 0)) for r in rows)
    p, lo, hi = wilson_ci(stale, len(rows))
    return {"runs": len(rows), "rate": p, "ci95": [lo, hi]}


def summarize_e13(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    by_fault: dict[str, list] = {}
    for r in rows:
        by_fault.setdefault(r.get("fault", "unknown"), []).append(r)
    out = {}
    for fault, sub in sorted(by_fault.items()):
        passed = sum(1 for r in sub if str(r.get("pass", "")).lower() in ("true", "1"))
        out[fault] = {"runs": len(sub), "pass_rate": passed / len(sub)}
    return out


def summarize_e14(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    out: dict[str, dict] = {}
    for scenario in sorted(set(r["scenario"] for r in rows)):
        for baseline in sorted(set(r["baseline"] for r in rows if r["scenario"] == scenario)):
            sub = [r for r in rows if r["scenario"] == scenario and r["baseline"] == baseline]
            stale = sum(int(r.get("stale_executed", 0)) for r in sub)
            p, lo, hi = wilson_ci(stale, len(sub))
            out.setdefault(scenario, {})[baseline] = {"runs": len(sub), "stale_rate": p, "stale_ci95": [lo, hi]}
    return out


def summarize_a1(path: Path) -> dict:
    rows = load_csv(path)
    if not rows:
        return {}
    out = {}
    for arm in sorted(set(r.get("arm", r.get("baseline", "")) for r in rows)):
        sub = [r for r in rows if r.get("arm", r.get("baseline")) == arm]
        n = len(sub)
        stale = sum(1 for r in sub if str(r.get("stale_executed", "0")) in ("1", "True", "true"))
        valid = sum(1 for r in sub if str(r.get("schema_valid", "1")) in ("1", "True", "true"))
        ser_p, ser_lo, ser_hi = wilson_ci(stale, n)
        out[arm] = {
            "attempted": n,
            "SER": ser_p,
            "SER_ci95": [ser_lo, ser_hi],
            "schema_validity_rate": valid / n if n else 0,
        }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=RESULTS / "paper_metrics_summary.json")
    args = parser.parse_args()

    summary = {
        "e1_baselines": summarize_e1(RESULTS / "e1_baselines.csv"),
        "fpr": summarize_fpr(RESULTS / "e1_fpr.csv"),
        "e3": summarize_e3(RESULTS / "e3_conflict_http.csv"),
        "e4_http": summarize_e4(RESULTS / "e4_load_http.csv"),
        "e6_network": summarize_e6(RESULTS / "e6_network.csv"),
        "e8_cell": summarize_e8(RESULTS / "e8_gazebo_cell.csv"),
        "e8_gazebo_sim": summarize_e8(RESULTS / "e8_gazebo_cell_sim.csv")
        if (RESULTS / "e8_gazebo_cell_sim.csv").exists()
        else None,
        "e9_shared_context": summarize_e9(RESULTS / "e9_shared_context.csv"),
    }
    for path, key, fn in (
        (RESULTS / "e9_consistency_sweep.csv", "e9_consistency_sweep", summarize_e9_sweep),
        (RESULTS / "e10_toctou.csv", "e10_toctou", summarize_e10),
        (RESULTS / "e11_stratified.csv", "e11_stratified", summarize_e11),
        (RESULTS / "e15_opcua_hil.csv", "e15_opcua_hil", lambda p: summarize_generic_rate(p, "stale_executed")),
    ):
        if path.exists():
            summary[key] = fn(path)
    if (RESULTS / "e12_scaling.csv").exists():
        summary["e12_scaling"] = load_csv(RESULTS / "e12_scaling.csv")
    if (RESULTS / "e13_faults.csv").exists():
        summary["e13_faults"] = summarize_e13(RESULTS / "e13_faults.csv")
    if (RESULTS / "e14_variants.csv").exists():
        summary["e14_variants"] = summarize_e14(RESULTS / "e14_variants.csv")
    e7 = RESULTS / "e7_faults.json"
    if e7.exists():
        summary["e7_faults"] = json.loads(e7.read_text())
    e0 = RESULTS / "e0_lifecycle.json"
    if e0.exists():
        summary["e0_lifecycle"] = json.loads(e0.read_text())
    if (RESULTS / "a1_baselines.csv").exists():
        summary["a1_baselines"] = summarize_a1(RESULTS / "a1_baselines.csv")
    if (RESULTS / "a2_latency_sweep.csv").exists():
        summary["a2_latency"] = load_csv(RESULTS / "a2_latency_sweep.csv")
    if (RESULTS / "a3_agent_loop.csv").exists():
        summary["a3_agent"] = load_csv(RESULTS / "a3_agent_loop.csv")
    a4 = RESULTS / "a4_evidence_audit.json"
    if a4.exists():
        summary["a4_audit"] = json.loads(a4.read_text())
    args.out.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
