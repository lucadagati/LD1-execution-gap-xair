"""E20 paired design: every decision reaches every gate with identical checks, and the
paired summary counts only decisions whose ground truth is the same for all gates."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

import run_e20_mes as e20  # noqa: E402


def test_paired_intents_share_everything_a_gate_checks():
    batch = e20.paired_intents("machine_7", "2026-10-06T00:00:00+00:00", 0.21, "dec-1")
    assert [(m, s) for m, s, _ in batch] == list(e20.GATES)
    intents = [i for _, _, i in batch]
    for key in ("timestamp_decision", "freshness_window_ms", "preconditions", "source"):
        assert len({repr(i[key]) for i in intents}) == 1
    assert {i["payload"]["parameters"]["decision_id"] for i in intents} == {"dec-1"}
    # distinct ids and targets: no shared lock or idempotency record between gates
    assert len({i["id"] for i in intents}) == len(intents)
    assert len({i["payload"]["target_entity"] for i in intents}) == len(intents)


def _row(dec, mode, scope, obsolete, released, ambiguous=0, L=30.0):
    return {"latency_min": L, "decision_id": dec, "mode": mode, "scope": scope or "",
            "obsolete": obsolete, "ambiguous": ambiguous, "gateway_released": released,
            "stale_release": int(released and obsolete),
            "false_revocation": int(not released and not obsolete and not ambiguous)}


def _decision(dec, obsolete, released_by):
    return [_row(dec, m, s, obsolete, int(e20.gate_label(m, s) in released_by)) for m, s in e20.GATES]


def test_paired_summary_counts_concordant_decisions_per_gate():
    rows = []
    rows += _decision("a", 1, {"direct"})                           # obsolete: only direct releases
    rows += _decision("b", 0, {g for g in map(lambda x: e20.gate_label(*x), e20.GATES) if g != "xair/global"})
    rows += _decision("c", 1, {"direct", "xair/global"})
    # discordant: a replay write landed between the gates' checks
    d = _decision("d", 0, {"direct"})
    d[1]["obsolete"] = 1
    rows += d
    cell = e20.summarize_paired(rows)["30.0"]
    assert cell["decisions"] == 4 and cell["label_discordant"] == 1
    assert cell["obsolete"] == 2 and cell["valid"] == 1
    assert cell["gates"]["direct"] == {"stale_released": 2, "false_revocations": 0}
    assert cell["gates"]["xair/global"] == {"stale_released": 1, "false_revocations": 1}
    assert cell["gates"]["xair/truth"] == {"stale_released": 0, "false_revocations": 0}


def test_incomplete_decisions_are_excluded():
    rows = _decision("a", 1, {"direct"})[:-1]                     # one gate's answer missing
    cell = e20.summarize_paired(rows)["30.0"]
    assert cell["decisions"] == 0 and cell["obsolete"] == 0
