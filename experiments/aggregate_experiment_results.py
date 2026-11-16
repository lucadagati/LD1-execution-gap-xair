#!/usr/bin/env python3
"""Aggregate every suite into the summary that backs the paper's numbers.

Rates carry Wilson 95% intervals; percentiles are nearest-rank (see common.py).
Sub-directories ``pinned/`` (E4/E12 on reserved cores) and ``distributed/``
(multi-node testbed) are summarized recursively under the same keys.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from common import RESULTS_DIR, percentile, truthy, wilson_ci

SUBSETS = ("pinned", "distributed", "physical", "lan", "scaling", "openplc")  # additional campaigns stored as sub-directories


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
    exact = {}
    if inj and "version_order" in inj[0]:
        rel_inj = [r for r in inj if r["gateway_released"] == "1"]
        exact = {
            "mode": inj[0].get("mode", "xair"),
            "released_before_check": sum(1 for r in rel_inj if r["version_order"] == "before_check"),
            "released_after_check": sum(1 for r in rel_inj if r["version_order"] == "after_check"),
            "blocked_before_check": sum(1 for r in inj if r["gateway_released"] == "0" and r["version_order"] == "before_check"),
            "blocked_after_check": sum(1 for r in inj if r["gateway_released"] == "0" and r["version_order"] == "after_check"),
            "unknown_order": sum(1 for r in inj if r["version_order"] == "unknown"),
            "stale_exact": rate(sum(int(r["stale_exact"]) for r in inj), len(inj)),
            "potential_stale": sum(int(r["potential_stale_publish"]) for r in inj),
        }
    out = {
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
    if exact:
        out["version_ordering"] = exact
    if inj and "position_vs_middleware" in inj[0]:
        rel_after = [r for r in inj if r["gateway_released"] == "1" and r["version_order"] == "after_check"]
        pos: dict[str, int] = defaultdict(int)
        for r in rel_after:
            pos[r["position_vs_middleware"] or "unknown"] += 1
        released_rows = [r for r in rows if r["gateway_released"] == "1"]
        out["position_vs_middleware"] = dict(pos)
        out["residual_bounds_ms"] = {
            "lo": lat([v for r in released_rows if (v := fnum(r, "residual_lo_ms")) is not None]),
            "hi": lat([v for r in released_rows if (v := fnum(r, "residual_hi_ms")) is not None]),
        }
    return out


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


def e16(rows: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[f"{r['version_scope']}|{r['pattern']}|{float(r['target_rate_hz']):g}"].append(r)
    return {
        k: {
            "fpr": rate(sum(1 - int(r["gateway_released"]) for r in v), len(v)),
            "achieved_rate_hz": float(v[0]["achieved_rate_hz"]),
            "goodput_ips": float(v[0]["goodput_ips"]),
            "e2e_ms": lat([float(r["e2e_latency_ms"]) for r in v]),
            "validation_to_gate_ms": lat([float(r["validation_to_gate_ms"]) for r in v if r.get("validation_to_gate_ms")]),
            "revoke_reasons": dict(sorted({x: sum(1 for r in v if r["reason"] == x and r["gateway_released"] == "0")
                                           for x in {r["reason"] for r in v if r["gateway_released"] == "0"}}.items())),
        }
        for k, v in sorted(groups.items())
    }


def e16_trace(rows: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[f"{float(r['publishing_interval_ms']):g}|{r['version_scope']}|{r['intent_kind']}"].append(r)
    return {k: {"fpr": rate(sum(1 - int(r["gateway_released"]) for r in v), len(v)),
                "achieved_update_rate_hz": float(v[0].get("achieved_update_rate_hz") or 0),
                "validation_to_gate_ms": lat([float(r["validation_to_gate_ms"]) for r in v if r.get("validation_to_gate_ms")])}
            for k, v in sorted(groups.items())}


def e10_deadline(rows: list[dict]) -> dict:
    out = {}
    for mode in sorted({r["mode"] for r in rows}):
        cell = [r for r in rows if r["mode"] == mode]
        rel = [r for r in cell if r["gateway_released"] == "1"]
        d = float(cell[0]["deadline_ms"])
        ages_rel = [v for r in rel if (v := fnum(r, "age_at_check_ms")) is not None]
        ages_m = [v for r in rel if (v := fnum(r, "age_at_middleware_ms")) is not None]
        out[mode] = {
            "trials": len(cell), "released": len(rel), "deadline_ms": d,
            "late_at_check": sum(int(r["late_at_check"]) for r in cell),
            "late_at_middleware": sum(int(r["late_at_middleware"]) for r in cell),
            "max_age_at_check_released_ms": max(ages_rel, default=None),
            "max_age_at_middleware_ms": max(ages_m, default=None),
            "check_to_middleware_ms": lat([m - c for r in rel
                                           if (m := fnum(r, "age_at_middleware_ms")) is not None
                                           and (c := fnum(r, "age_at_check_ms")) is not None]),
            "revoked_reasons": dict(sorted({(r["reason"] or "").split(":")[0]: 0 for r in cell}.items())),
        }
        for r in cell:
            if r["gateway_released"] == "0":
                key = (r["reason"] or "").split(":")[0]
                out[mode]["revoked_reasons"][key] = out[mode]["revoked_reasons"].get(key, 0) + 1
        out[mode]["revoked_reasons"] = {k: v for k, v in out[mode]["revoked_reasons"].items() if v}
    return out


def e18(rows: list[dict]) -> dict:
    out = {}
    for name in sorted({r["scenario"] for r in rows}):
        cell = [r for r in rows if r["scenario"] == name]
        num = lambda k: sum(int(r[k]) for r in cell if r.get(k) not in (None, ""))
        out[name] = {"reps": len(cell), "committed": num("log_records"), "effects": num("effects"),
                     "duplicate_effects": num("duplicate_effects"), "lost": num("lost"),
                     "suppressed_replays": num("suppressed_replays"),
                     **{k: num(k) for k in ("refused_duplicates", "effects_in_commit_order",
                                            "commit_versions_monotone", "flagged", "withheld") if cell[0].get(k) not in (None, "")}}
    return out


def _aba_class(r: dict) -> str:
    """Recompute the store-order class of an E10-ABA flip trial from its version columns."""
    if "pause_version" not in r:
        return "unknown"            # campaigns recorded before the version columns existed
    iv = lambda k: int(r[k]) if r.get(k) not in (None, "", "None") else None
    v_val, vp, vr, v_chk = iv("validation_version"), iv("pause_version"), iv("resume_version"), iv("check_version")
    if vp is not None and v_val is not None and vp <= v_val:
        return "flip_outside_window"
    if vr is not None and v_chk is not None and vr > v_chk:
        return "flip_outside_window"
    if None in (v_val, vp, vr, v_chk):
        return "unknown"
    return "valid" if v_val < vp < vr <= v_chk else "flip_outside_window"


def e10_aba(rows: list[dict]) -> dict:
    out = {}
    for key in sorted({f"{r['mode']}|{r['scope']}" for r in rows}):
        m, s = key.split("|")
        allflip = [r for r in rows if r["mode"] == m and r["scope"] == s and r["flip"] == "1"]
        # trials classified by store version whose flip fell outside (t_v, check] test no history violation
        flip = [r for r in allflip if _aba_class(r) != "flip_outside_window"]
        ctl = [r for r in rows if r["mode"] == m and r["scope"] == s and r["flip"] == "0"]
        out[key] = {"released_after_undone_change": rate(sum(int(r["gateway_released"]) for r in flip), len(flip)),
                    "controls_released": rate(sum(int(r["gateway_released"]) for r in ctl), len(ctl)),
                    "flip_outside_window": len(allflip) - len(flip)}
    return out


def e21(rows: list[dict]) -> dict:
    out = {}
    for key in sorted({f"{r['mode']}|{r['actuator']}" for r in rows}):
        m, a = key.split("|")
        inj = [r for r in rows if r["mode"] == m and r["actuator"] == a and r["inject"] == "1"]
        ctl = [r for r in rows if r["mode"] == m and r["actuator"] == a and r["inject"] == "0"]
        offsets = sorted({float(r["inject_offset_ms"]) for r in inj})
        out[key] = {"stale_effects": rate(sum(int(r["stale_effect"]) for r in inj), len(inj)),
                    "history_violations": rate(sum(int(r.get("history_violation") or 0) for r in inj), len(inj)),
                    "executed": sum(int(r["executed"]) for r in inj),
                    "refused_by_controller": sum(int(r["refused_by_controller"]) for r in inj),
                    "blocked_by_xair": sum(1 for r in inj if r["gateway_released"] == "0"),
                    "controls_executed": rate(sum(int(r["executed"]) for r in ctl), len(ctl)),
                    "e2e_ms_controls": lat([v for r in ctl if (v := fnum(r, "e2e_latency_ms")) is not None]),
                    # stale effects per pause offset: the window in which a pause escapes XAIR's check
                    "by_offset": {f"{o:g}": rate(sum(int(r["stale_effect"]) for r in inj if float(r["inject_offset_ms"]) == o),
                                                 sum(1 for r in inj if float(r["inject_offset_ms"]) == o))
                                  for o in offsets}}
    return out


def e20(rows: list[dict]) -> dict:
    out = {}
    for key in sorted({f"{float(r['latency_min']):g}|{r['mode']}|{r['scope']}" for r in rows}):
        L, m, s = key.split("|")
        cell = [r for r in rows if f"{float(r['latency_min']):g}" == L and r["mode"] == m and r["scope"] == s]
        ob = [r for r in cell if r["obsolete"] == "1"]
        va = [r for r in cell if r["obsolete"] == "0"]
        out[key] = {"decisions": len(cell), "obsolete": len(ob),
                    "stale_released": rate(sum(int(r["stale_release"]) for r in ob), len(ob)),
                    "false_revocations": rate(sum(int(r["false_revocation"]) for r in va), len(va)),
                    "update_rate_hz": float(cell[0].get("update_rate_hz") or 0) if cell else 0.0}
    return out


def spread(values: list[float]) -> dict:
    return {"n": len(values), "mean": statistics.fmean(values) if values else None,
            "sd": statistics.stdev(values) if len(values) > 1 else 0.0,
            "min": min(values, default=None), "max": max(values, default=None), "values": values}


def e20_paired(rows: list[dict]) -> dict:
    """E20 paired design: every decision through every gate (run_e20_mes.py --design paired).

    Per latency: complete decisions, decisions whose ground truth differs between gates
    (excluded), and per gate the stale releases over the decisions obsolete for every gate
    and the false revocations over those valid for every gate (ambiguous ones count as neither)."""
    out: dict = {}
    gates = sorted({f"{r['mode']}|{r['scope']}" for r in rows})
    for L in sorted({float(r["latency_min"]) for r in rows}):
        by_dec: dict[str, list[dict]] = {}
        for r in rows:
            if float(r["latency_min"]) == L:
                by_dec.setdefault(r["decision_id"], []).append(r)
        complete = {d: rs for d, rs in by_dec.items() if sorted(f"{r['mode']}|{r['scope']}" for r in rs) == gates}
        concordant = {d: rs for d, rs in complete.items() if len({(r["obsolete"], r["ambiguous"]) for r in rs}) == 1}
        ob = [rs for rs in concordant.values() if rs[0]["obsolete"] == "1"]
        va = [rs for rs in concordant.values() if rs[0]["obsolete"] == "0" and rs[0]["ambiguous"] == "0"]
        cell = {"decisions": len(complete), "label_discordant": len(complete) - len(concordant),
                "obsolete": len(ob), "valid": len(va),
                "update_rate_hz": float(rows[0].get("update_rate_hz") or 0) if rows else 0.0, "gates": {}}
        for g in gates:
            pick = lambda rs: next(r for r in rs if f"{r['mode']}|{r['scope']}" == g)  # noqa: E731
            cell["gates"][g] = {"stale_released": rate(sum(int(pick(rs)["stale_release"]) for rs in ob), len(ob)),
                                "false_revocations": rate(sum(int(pick(rs)["false_revocation"]) for rs in va), len(va))}
        out[f"{L:g}"] = cell
    return out


def across_campaigns(builds: list[dict]) -> dict:
    """Between-campaign spread of the timing-sensitive metrics (one value per campaign)."""
    out: dict = {}
    def collect(name, fn):
        vals = [v for b in builds if (v := fn(b)) is not None]
        if vals:
            out[name] = spread(vals)
    for key in ("e10_boundary", "e10_atomic", "e10_natural", "e10_natural_atomic"):
        collect(f"{key}.stale_exact", lambda b, k=key: b.get(k, {}).get("version_ordering", {}).get("stale_exact", {}).get("k"))
        collect(f"{key}.potential_stale", lambda b, k=key: b.get(k, {}).get("version_ordering", {}).get("potential_stale"))
        collect(f"{key}.stale_at_middleware", lambda b, k=key: b.get(k, {}).get("position_vs_middleware", {}).get("stale_at_middleware", 0) if k in b else None)
        collect(f"{key}.residual_lo_p50_ms", lambda b, k=key: b.get(k, {}).get("residual_bounds_ms", {}).get("lo", {}).get("p50"))
        collect(f"{key}.residual_hi_p50_ms", lambda b, k=key: b.get(k, {}).get("residual_bounds_ms", {}).get("hi", {}).get("p50"))
        collect(f"{key}.validation_to_release_p50_ms", lambda b, k=key: b.get(k, {}).get("validation_to_release_ms_controls", {}).get("p50"))
    for key in ("e16", "e16_trace"):
        cells = sorted({c for b in builds for c in b.get(key, {})})
        for c in cells:
            collect(f"{key}.{c}.fpr", lambda b, k=key, c=c: b.get(k, {}).get(c, {}).get("fpr", {}).get("rate"))
    for key in sorted({k for b in builds for k in b.get("e10_aba", {})}):
        collect(f"e10_aba.{key}.released", lambda b, k=key: b.get("e10_aba", {}).get(k, {}).get("released_after_undone_change", {}).get("rate"))
    for key in sorted({k for b in builds for k in b.get("e21", {})}):
        collect(f"e21.{key}.stale", lambda b, k=key: b.get("e21", {}).get(k, {}).get("stale_effects", {}).get("rate"))
    for mode in sorted({m for b in builds for m in b.get("e10_deadline", {})}):
        collect(f"e10_deadline.{mode}.late_at_check", lambda b, m=mode: b.get("e10_deadline", {}).get(m, {}).get("late_at_check"))
        collect(f"e10_deadline.{mode}.late_at_middleware", lambda b, m=mode: b.get("e10_deadline", {}).get(m, {}).get("late_at_middleware"))
    return out


def model_fit(cells: list[tuple[float, float, float]]) -> dict:
    """Largest gap between observed FPR and the Poisson / periodic predictions over (rate_hz, gate_s, fpr) cells."""
    import math
    pois = [abs(1 - math.exp(-lam * g) - o) for lam, g, o in cells]
    per = [abs(min(1.0, lam * g) - o) for lam, g, o in cells]
    return {"cells": len(cells), "max_abs_err_poisson": max(pois, default=None), "max_abs_err_periodic": max(per, default=None)}


def _fit_cells(build: dict) -> dict:
    """Global-version cells with a non-zero rate (E16) and version-scoped trace cells (E16-trace)."""
    # cells whose writers could not reach the target rate (saturated, writer and gate contend
    # for XAIR) are excluded: the arrival models assume updates independent of the gate
    e16 = [(v["achieved_rate_hz"], v["validation_to_gate_ms"]["mean"] / 1000, v["fpr"]["rate"])
           for k, v in build.get("e16", {}).items() if k.startswith("global|") and not k.endswith("|0")
           and v["achieved_rate_hz"] >= 0.8 * float(k.split("|")[2])]
    tr = [(v["achieved_update_rate_hz"], v["validation_to_gate_ms"]["mean"] / 1000, v["fpr"]["rate"])
          for k, v in build.get("e16_trace", {}).items() if k.split("|")[1] == "global" or k.endswith("readset|continuous")]
    return {"e16": e16, "e16_trace": tr}


def build_summary(R: Path) -> dict:
    summary = _build(R)
    if summary.get("e16"):
        summary["model_fit"] = {"e16": model_fit(_fit_cells(summary)["e16"])}
    for sub in SUBSETS:
        if (R / sub).is_dir():
            summary[sub] = _build(R / sub)
            camp = sorted(p for p in (R / sub / "campaigns").glob("c*") if p.is_dir()) if (R / sub / "campaigns").is_dir() else []
            if camp:
                builds = {"c1": summary[sub], **{p.name: _build(p) for p in camp}}
                summary[sub]["campaigns"] = {k: v for k, v in builds.items() if k != "c1"}
                summary[sub]["across_campaigns"] = across_campaigns(list(builds.values()))
                cells = [_fit_cells(b) for b in builds.values()]
                summary[sub]["model_fit"] = {
                    "e16_per_campaign": model_fit([c for x in cells for c in x["e16"]]),
                    "e16_trace_per_campaign": model_fit([c for x in cells for c in x["e16_trace"]]),
                }
            if (R / sub / "sensitivity").is_dir():
                summary[sub]["sensitivity"] = {p.name: _build(p) for p in sorted((R / sub / "sensitivity").iterdir()) if p.is_dir()}
            if not camp and summary[sub].get("e16"):
                summary[sub]["model_fit"] = {"e16": model_fit(_fit_cells(summary[sub])["e16"])}
    # E25: one directory per embedded board
    if (R / "embedded").is_dir():
        summary["embedded"] = {p.name: _build(p) for p in sorted((R / "embedded").iterdir())
                               if p.is_dir() and (p / "environment.txt").exists()}
        for name, b in summary["embedded"].items():
            env = [json.loads(x) for x in (R / "embedded" / name / "environment.txt").read_text().splitlines() if x.strip().startswith("{")]
            b["environment"] = {k: v for e in env for k, v in e.items()}
    return summary


def _build(R: Path) -> dict:
    summary: dict = {}
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
    if rows := load(R / "e16_context_churn.csv"):
        summary["e16"] = e16(rows)
    e6_runs = {p.stem: e6(load(p)) for p in sorted(R.glob("e6_network*.csv")) if load(p)}
    if e6_runs:
        summary["e6"] = e6_runs
    e8_paths = sorted(R.glob("e8_gazebo_campaign*.csv"))
    if e8_paths:
        summary["e8_gazebo"] = e8(e8_paths)
    if rows := load(R / "e15_opcua_hil.csv"):
        summary["e15"] = {"by_mode": grouped_rate(rows, ("mode",), "stale_executed"),
                          "transport": sorted({r["transport"] for r in rows})}
    if rows := load(R / "e10_toctou_atomic.csv"):
        summary["e10_atomic"] = e10(rows)
    if rows := load(R / "e10_natural_window.csv"):
        summary["e10_natural"] = e10(rows)
    if rows := load(R / "e10_natural_window_atomic.csv"):
        summary["e10_natural_atomic"] = e10(rows)
    if rows := load(R / "e16_trace_churn.csv"):
        summary["e16_trace"] = e16_trace(rows)
    if rows := load(R / "e10_deadline.csv"):
        summary["e10_deadline"] = e10_deadline(rows)
    if rows := load(R / "e17_policy.csv"):
        summary["e17"] = grouped_rate(rows, ("policy", "kind"), "gateway_released")
    if rows := load(R / "e18_outbox_faults.csv"):
        summary["e18"] = e18(rows)
    if rows := load(R / "e18b_durability.csv"):
        summary["e18b"] = {k: sum(int(r[k]) for r in rows) for k in rows[0] if k != "rep"} | {"reps": len(rows)}
    if rows := load(R / "e10_aba.csv"):
        summary["e10_aba"] = e10_aba(rows)
    if rows := load(R / "e21_cell_controller.csv"):
        summary["e21"] = e21(rows)
    if rows := load(R / "e21_cell_aba.csv"):
        summary["e21_aba"] = e21(rows)
    if rows := load(R / "e24_openplc.csv"):
        summary["e24"] = e21(rows)
    if rows := load(R / "e23_truth_scaling.csv"):
        summary["e23"] = [{k: (float(v) if k not in ("layout",) else v) for k, v in r.items()} for r in rows]
    if rows := load(R / "e23_retirement.csv"):
        summary["e23_retirement"] = rows
    if rows := load(R / "e20_mes_replay.csv"):
        summary["e20"] = e20(rows)
    if rows := load(R / "e20_paired" / "e20_mes_replay_paired.csv"):
        summary["e20_paired"] = e20_paired(rows)
    if rows := load(R / "e20_mes_sensitivity.csv"):
        summary["e20_sensitivity"] = {f"{float(r['latency_s']):g}|{r['model']}|{r['proxy']}": {
            k: float(r[k]) for k in ("fraction", "per_resource_min", "per_resource_median", "per_resource_max") if r.get(k) not in (None, "")}
            for r in rows}
    if rows := load(R / "e20_mes_offline.csv"):
        summary["e20_offline"] = {f"{float(r['latency_s']):g}": {"decisions": int(r["decisions"]), "obsolete": int(r["obsolete"]),
                                                                  "obsolete_fraction": float(r["obsolete_fraction"]),
                                                                  **({"interior_fraction": float(r["exact_interior_fraction"])}
                                                                     if r.get("exact_interior_fraction") else {})} for r in rows}
    reps = [load(p)[0] for p in sorted(R.glob("e4_load_http_rep*.csv")) if load(p)]
    if reps:
        summary["e4_reps"] = [{k: float(v) for k, v in r.items()} for r in reps]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    out_path = args.out or args.results / "paper_metrics_summary.json"
    summary = build_summary(args.results)
    out_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
