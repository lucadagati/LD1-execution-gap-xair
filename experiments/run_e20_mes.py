#!/usr/bin/env python3
"""E20: prevalence of drift in a real production event log, and the gates on it.

Offline: for decisions taken at random instants on an IDLE machine of the
4TU job-shop log (mes_trace.py), the fraction whose machine is no longer IDLE
after a decision-to-release latency L, for L from 1 s to 1 h (exact, from the
log).

Live: ``--days`` of the log are replayed ``--compression`` times faster into
the XAIR snapshot (one update per machine state change). A scheduler decides
"start a job on machine m" for a machine that is IDLE at the decision instant
(ground truth from the log) and waits a latency equivalent to ``--latency-min``
minutes of plant time. Ground truth at the middleware call (or at the gate's
check, if it withheld) is the state of m in the snapshot as written by the
replay; the log's state is kept for reference.

Two designs:

* ``--design paired`` (default since v1.7): every decision is submitted through
  *every* gate, concurrently after the same wait, with the same decision time,
  machine and precondition (distinct ids and targets), so gates are compared on
  the same decisions. Rows carry ``decision_id``; a decision whose ground truth
  differs between gates (a replay write landed between their checks) is flagged
  ``label_discordant`` and excluded from the paired counts.
* ``--design rotation``: each decision goes through one gate, in rotation (the
  design of the v1.6 data, ``data/execution-gap/distributed/e20_mes_replay.csv``).
"""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common import RESULTS_DIR, XAIR, adapter, http_json, released, write_csv
from mes_trace import Timeline

GATES = (("direct", None), ("xair", "global"), ("xair", "readset"), ("xair", "truth"), ("xair_atomic", "truth"))


def gate_label(mode: str, scope: str | None) -> str:
    return f"{mode}/{scope}" if scope else mode


def paired_intents(machine: str, t_d_iso: str, L_wall: float, decision_id: str, gates=GATES) -> list[tuple]:
    """One intent per gate for the same decision: same t_d, machine, precondition and window.

    Ids and targets differ, so the gates never share a lock or an idempotency
    record; everything a gate checks is identical."""
    out = []
    for mode, scope in gates:
        out.append((mode, scope, {
            "id": str(uuid.uuid4()), "source": "mes", "timestamp_decision": t_d_iso,
            "freshness_window_ms": int(L_wall * 1000) + 60000,
            "preconditions": [{"expr": f"mes.{machine}.state == 'IDLE'"}],
            "payload": {"action_type": "START_JOB", "target_entity": f"{machine}_{uuid.uuid4().hex[:6]}",
                        "parameters": {"decision_id": decision_id}},
        }))
    return out


def summarize_paired(rows: list[dict], gates=GATES) -> dict:
    """Paired comparison per latency: decisions, label-discordant decisions, and per gate the
    stale releases over the decisions obsolete for every gate and the false revocations over
    those valid for every gate (ambiguous rows count as neither)."""
    out: dict = {}
    for L in sorted({float(r["latency_min"]) for r in rows}):
        by_dec: dict[str, list[dict]] = {}
        for r in rows:
            if float(r["latency_min"]) == L:
                by_dec.setdefault(str(r["decision_id"]), []).append(r)
        all_gates = {gate_label(m, s) for m, s in gates}
        complete = {d: rs for d, rs in by_dec.items()
                    if len(rs) == len(all_gates) and {gate_label(r["mode"], r["scope"] or None) for r in rs} == all_gates}
        concordant, discordant = {}, 0
        for d, rs in complete.items():
            labels = {(int(r["obsolete"]), int(r["ambiguous"])) for r in rs}
            if len(labels) == 1:
                concordant[d] = rs
            else:
                discordant += 1
        labels = sorted(all_gates)
        cell = {"decisions": len(complete), "label_discordant": discordant,
                "obsolete": sum(1 for rs in concordant.values() if int(rs[0]["obsolete"])),
                "valid": sum(1 for rs in concordant.values() if not int(rs[0]["obsolete"]) and not int(rs[0]["ambiguous"])),
                "gates": {}}
        for g in labels:
            stale = falser = 0
            for rs in concordant.values():
                r = next((x for x in rs if gate_label(x["mode"], x["scope"] or None) == g), None)
                if r is None:
                    continue
                stale += int(r["stale_release"])
                falser += int(r["false_revocation"])
            cell["gates"][g] = {"stale_released": stale, "false_revocations": falser}
        out[str(L)] = cell
    return out


def offline_exact(tl: Timeline, L: float, interior_only: bool = False) -> tuple[float, float]:
    """Exact (obsolete measure, idle measure) over decision instants t in [0, span - L] on idle machines.

    A decision at t on machine m is obsolete if m leaves IDLE within (t, t + L].
    For an idle interval [a, b) ending with a state change at b, the obsolete
    instants are [max(a, b - L), b); summing over intervals and machines gives
    the exact fraction under decisions uniform over idle machine-time.

    The log does not say whether a machine is idle before its first recorded
    step or after its last one; the timeline treats both as idle. With
    ``interior_only`` those censored intervals are excluded."""
    horizon = tl.span - L
    obs = idle = 0.0
    for m in tl.machines:
        times, states = tl.changes[m]
        for k, (a, s) in enumerate(zip(times, states)):
            if s != "IDLE":
                continue
            b = times[k + 1] if k + 1 < len(times) else float("inf")
            if interior_only and (k == 0 or b == float("inf")):
                continue
            lo, hi = max(a, 0.0), min(b, horizon)
            if hi <= lo:
                continue
            idle += hi - lo
            if b != float("inf"):
                olo = max(lo, b - L)
                if hi > olo:
                    obs += hi - olo
    return obs, idle


def offline(tl: Timeline, latencies_s: list[float], samples: int, rng: random.Random) -> list[dict]:
    """Exact fractions, with a Monte Carlo estimate (Wilson 95% interval) as a cross-check."""
    from common import wilson_ci
    rows = []
    for L in latencies_s:
        obsolete = n = 0
        while n < samples:
            m = rng.choice(tl.machines)
            t = rng.uniform(0, tl.span - L)
            if tl.state(m, t) != "IDLE":
                continue
            n += 1
            obsolete += int(tl.next_change(m, t) <= t + L)
        o, i = offline_exact(tl, L)
        oi, ii = offline_exact(tl, L, interior_only=True)
        lo, hi = wilson_ci(obsolete, n)
        rows.append({"latency_s": L, "exact_obsolete_fraction": o / i, "idle_machine_seconds": i,
                     "exact_interior_fraction": oi / ii, "interior_idle_machine_seconds": ii,
                     "mc_decisions": n, "mc_obsolete": obsolete, "mc_fraction": obsolete / n,
                     "mc_ci95_lo": lo, "mc_ci95_hi": hi,
                     # kept for the summary key used so far
                     "decisions": n, "obsolete": obsolete, "obsolete_fraction": o / i})
    return rows


def _idle_intervals(tl: Timeline, m: str):
    """Interior idle intervals [a, b) of machine m with the steps that start at b."""
    times, states = tl.changes[m]
    steps = tl.steps[m]
    starts = [st[0] for st in steps]
    for k, (a, s) in enumerate(zip(times, states)):
        if s != "IDLE" or k == 0 or k + 1 >= len(times):
            continue
        b = times[k + 1]
        i = bisect.bisect_left(starts, b)
        nxt = [st for st in steps[i:] if st[0] == b]
        prev = [st for st in steps[:i] if st[1] <= a]
        yield a, b, nxt, (prev[-1] if prev else None)


def sensitivity(tl: Timeline, latencies_s: list[float]) -> list[dict]:
    """Drift fraction under alternative decision models and invalidation proxies.

    Interior idle intervals only. ``uniform``: decisions uniform over idle time;
    ``uniform_le8h``: idle intervals longer than 8 h (nights, weekends) excluded;
    ``idle_start``: one decision at the start of each idle interval, as a
    scheduler that dispatches as soon as a machine frees up. Proxies: ``any``,
    the machine leaves IDLE; ``breakdown``, the next step is a breakdown (B);
    ``variant``, the next step is for a different part than the previous one.
    The log has one-minute timestamps, so latencies below a minute are not resolved."""
    def proxy_hit(kind: str, nxt, prev) -> bool:
        if kind == "any":
            return True
        if kind == "breakdown":
            return any(st[2] == "B" for st in nxt)
        return prev is not None and any(st[3] != prev[3] for st in nxt)

    rows = []
    for L in latencies_s:
        for model in ("uniform", "uniform_le8h", "idle_start"):
            for kind in ("any", "breakdown", "variant"):
                obs = tot = 0.0
                per: dict[str, list[float]] = {}
                for m in tl.machines:
                    o_m = t_m = 0.0
                    for a, b, nxt, prev in _idle_intervals(tl, m):
                        length = b - a
                        if length <= 0:
                            continue
                        if model == "uniform_le8h" and length > 8 * 3600:
                            continue
                        hit = proxy_hit(kind, nxt, prev)
                        if model == "idle_start":
                            t_m += 1
                            o_m += float(hit and length <= L)
                        else:
                            t_m += length
                            o_m += min(L, length) if hit else 0.0
                    obs += o_m
                    tot += t_m
                    if t_m:
                        per[m] = o_m / t_m
                vals = sorted(per.values())
                rows.append({"latency_s": L, "model": model, "proxy": kind, "fraction": obs / tot if tot else 0.0,
                             "resources": len(vals), "per_resource_min": vals[0] if vals else "",
                             "per_resource_median": vals[len(vals) // 2] if vals else "",
                             "per_resource_max": vals[-1] if vals else ""})
    return rows


class Replayer:
    def __init__(self, tl: Timeline, t_from: float, t_to: float, compression: float) -> None:
        self.tl, self.t_from, self.t_to, self.c = tl, t_from, t_to, compression
        self.events = tl.events(t_from, t_to)
        self._stop = threading.Event()
        self.writes = 0
        self.lock = threading.Lock()
        self.written: dict[str, list[tuple[float, float, str]]] = {}

    def written_state(self, m: str, wall: float) -> tuple[str, str]:
        """State of m in the snapshot at wall time: (by writes completed before it, by writes started before it)."""
        with self.lock:
            ws = list(self.written.get(m, []))
        done = [s for a, b, s in ws if b <= wall]
        started = [s for a, b, s in ws if a <= wall]
        base = self.init.get(m, "IDLE")
        return (done[-1] if done else base), (started[-1] if started else base)

    def trace_time(self, wall: float) -> float:
        return self.t_from + (wall - self.wall0) * self.c

    def _run(self) -> None:
        for t, m, s in self.events:
            delay = self.wall0 + (t - self.t_from) / self.c - time.time()
            if delay > 0 and self._stop.wait(delay):
                return
            t_a = time.time()
            http_json(f"{XAIR}/v1/context/snapshot", {"mes": {m: {"state": s}}}, timeout=5)
            with self.lock:
                self.written.setdefault(m, []).append((t_a, time.time(), s))
            self.writes += 1

    def start(self) -> None:
        init = {m: {"state": self.tl.state(m, self.t_from)} for m in self.tl.machines}
        self.init = {m: v["state"] for m, v in init.items()}
        http_json(f"{XAIR}/v1/context/snapshot", {"mes": init}, timeout=5)
        self.wall0 = time.time()
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def stop(self) -> None:
        self._stop.set()
        self.th.join(timeout=10)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--start-day", type=float, default=0.0)
    ap.add_argument("--compression", type=float, default=8640.0, help="plant seconds per wall second")
    ap.add_argument("--latency-min", type=float, nargs="+", default=[30.0, 240.0])
    ap.add_argument("--intents", type=int, default=1000, help="Decisions per latency")
    ap.add_argument("--offline-samples", type=int, default=100000)
    ap.add_argument("--seed", type=int, default=20)
    ap.add_argument("--offline-only", action="store_true")
    ap.add_argument("--design", choices=("paired", "rotation"), default="paired",
                    help="paired: every decision through every gate; rotation: one gate per decision (v1.6 data)")
    ap.add_argument("--out", default=None,
                    help="default: e20_mes_replay_paired.csv (paired) or e20_mes_replay.csv (rotation)")
    args = ap.parse_args()
    args.out = args.out or str(RESULTS_DIR / ("e20_mes_replay_paired.csv" if args.design == "paired"
                                              else "e20_mes_replay.csv"))
    rng = random.Random(args.seed)
    tl = Timeline()
    off = offline(tl, [1, 10, 60, 300, 1800, 3600], args.offline_samples, rng)
    write_csv(Path(args.out).with_name("e20_mes_offline.csv"), off)
    write_csv(Path(args.out).with_name("e20_mes_sensitivity.csv"), sensitivity(tl, [60, 300, 1800, 3600]))
    print(json.dumps({"offline": off}))
    if args.offline_only:
        return 0

    t_from = args.start_day * 86400
    rows = []
    for L_min in args.latency_min:
        rep = Replayer(tl, t_from, t_from + args.days * 86400, args.compression)
        rep.start()
        span_wall = args.days * 86400 / args.compression
        gates = itertools.cycle(GATES)
        L_wall = L_min * 60 / args.compression
        lock, threads = threading.Lock(), []

        def decide(m: str, mode: str, scope: str | None) -> None:
            t_d = datetime.now(timezone.utc)
            intent = {"id": str(uuid.uuid4()), "source": "mes", "timestamp_decision": t_d.isoformat(),
                      "freshness_window_ms": int(L_wall * 1000) + 60000,
                      "preconditions": [{"expr": f"mes.{m}.state == 'IDLE'"}],
                      "payload": {"action_type": "START_JOB", "target_entity": f"{m}_{uuid.uuid4().hex[:6]}"}}
            time.sleep(L_wall)
            resp = adapter("intent", intent, mode=mode, version_scope=scope)
            rel = released(resp)
            t_m = resp.get("t_middleware_wall_ms")
            t_g = resp.get("t_gate_wall_ms")
            t_ref = (t_m / 1000.0) if (rel and t_m) else (t_g / 1000.0) if t_g else time.time()
            # ground truth: the snapshot as written by the replay (as in every other suite);
            # the log's own state at that trace time is kept for reference
            done, started = rep.written_state(m, t_ref)
            log_state = tl.state(m, rep.trace_time(t_ref))
            obsolete = done != "IDLE"
            ambiguous = (done == "IDLE") != (started == "IDLE")
            with lock:
                rows.append({"latency_min": L_min, "design": "rotation", "decision_id": intent["id"],
                             "mode": mode, "scope": scope or "", "machine": m,
                             "gateway_released": int(rel), "reason": resp.get("reason"),
                             "state_at_release_or_check": done, "state_in_log": log_state,
                             "obsolete": int(obsolete), "ambiguous": int(ambiguous),
                             "stale_release": int(rel and obsolete),
                             "false_revocation": int(not rel and not obsolete and not ambiguous),
                             "e2e_latency_ms": resp.get("e2e_latency_ms")})

        def submit_one(m, mode, scope, intent, decision_id):
            resp = adapter("intent", intent, mode=mode, version_scope=scope)
            rel = released(resp)
            t_m = resp.get("t_middleware_wall_ms")
            t_g = resp.get("t_gate_wall_ms")
            t_ref = (t_m / 1000.0) if (rel and t_m) else (t_g / 1000.0) if t_g else time.time()
            done, started = rep.written_state(m, t_ref)
            log_state = tl.state(m, rep.trace_time(t_ref))
            obsolete = done != "IDLE"
            ambiguous = (done == "IDLE") != (started == "IDLE")
            with lock:
                rows.append({"latency_min": L_min, "design": "paired", "decision_id": decision_id,
                             "mode": mode, "scope": scope or "", "machine": m,
                             "gateway_released": int(rel), "reason": resp.get("reason"),
                             "state_at_release_or_check": done, "state_in_log": log_state,
                             "obsolete": int(obsolete), "ambiguous": int(ambiguous),
                             "stale_release": int(rel and obsolete),
                             "false_revocation": int(not rel and not obsolete and not ambiguous),
                             "e2e_latency_ms": resp.get("e2e_latency_ms")})

        def decide_paired(m: str) -> None:
            decision_id = str(uuid.uuid4())
            batch = paired_intents(m, datetime.now(timezone.utc).isoformat(), L_wall, decision_id)
            time.sleep(L_wall)
            subs = [threading.Thread(target=submit_one, args=(m, mode, scope, intent, decision_id), daemon=True)
                    for mode, scope, intent in batch]
            for th in subs:
                th.start()
            for th in subs:
                th.join(timeout=60)

        for k in range(args.intents):
            target = rep.wall0 + (k + rng.random()) * (span_wall - L_wall - 2) / args.intents
            if (d := target - time.time()) > 0:
                time.sleep(d)
            now_tr = rep.trace_time(time.time())
            idle = [m for m in tl.machines if tl.state(m, now_tr) == "IDLE"]
            if not idle:
                continue
            if args.design == "paired":
                th = threading.Thread(target=decide_paired, args=(rng.choice(idle),), daemon=True)
            else:
                mode, scope = next(gates)
                th = threading.Thread(target=decide, args=(rng.choice(idle), mode, scope), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join(timeout=60)
        rep.stop()
        rate = rep.writes / span_wall
        for r in rows:
            if r["latency_min"] == L_min:
                r["update_rate_hz"] = round(rate, 2)
    write_csv(Path(args.out), rows)
    if args.design == "paired":
        print(json.dumps({"paired": summarize_paired(rows)}))
    for L_min, (mode, scope) in itertools.product(args.latency_min, GATES):
        cell = [r for r in rows if r["latency_min"] == L_min and r["mode"] == mode and r["scope"] == (scope or "")]
        ob = [r for r in cell if r["obsolete"]]
        va = [r for r in cell if not r["obsolete"]]
        print(json.dumps({"L_min": L_min, "gate": f"{mode}/{scope}", "n": len(cell), "obsolete": len(ob),
                          "stale_released": sum(r["stale_release"] for r in ob),
                          "false_revocations": f"{sum(r['false_revocation'] for r in va)}/{len(va)}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
