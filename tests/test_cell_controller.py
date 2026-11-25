"""Semantics of the running example on the emulated cell controller (scripts/cell_controller.py).

``line.state`` is the operator's run mode; RESUME restarts the conveyor and is
admissible only in RUN. The controller applies the declared effect
(conveyor RUNNING), a pause stops the conveyor, and the three command kinds
differ exactly as the paper states: unconditional commands act on a paused line
(stale effect), the interlock refuses on PAUSED but not after an undone pause,
the version-conditional command refuses both.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("asyncua")
_spec = importlib.util.spec_from_file_location(
    "cell_controller", Path(__file__).resolve().parents[1] / "scripts" / "cell_controller.py")
cell_controller = importlib.util.module_from_spec(_spec)
sys.modules["cell_controller"] = cell_controller
_spec.loader.exec_module(cell_controller)


def _run(cell, expected=None, deadline=None, needs_run=False) -> str:
    """Queue one command and execute it in one scan, as the scanner task does."""
    async def go():
        fut = asyncio.get_running_loop().create_future()
        cell.pending.append(("intent", expected, deadline, fut, needs_run))
        cell.scan()
        return await fut
    return asyncio.run(go())


def _cell():
    return cell_controller.Cell(None, version_seed=0)


def test_resume_restarts_the_conveyor_on_a_running_line():
    cell = _cell()
    assert (cell.state, cell.conveyor) == ("RUN", "STOPPED")
    res = _run(cell)
    assert res == "executed:RUN:0" and cell.conveyor == "RUNNING" and cell.effects == 1


def test_pause_stops_the_conveyor_and_advances_the_version():
    cell = _cell()
    _run(cell)
    assert cell.set_state("PAUSED") == 1 and cell.conveyor == "STOPPED"
    assert cell.set_state("PAUSED") == 1          # rewriting the same value does not advance it


def test_unconditional_resume_on_a_paused_line_is_a_stale_effect():
    cell = _cell()
    cell.set_state("PAUSED")
    res = _run(cell)
    # the effect the paper calls stale: the conveyor runs while the line is paused
    assert res.startswith("executed:PAUSED") and cell.conveyor == "RUNNING" and cell.state == "PAUSED"


def test_interlock_and_version_refuse_on_a_paused_line():
    cell = _cell()
    v = cell.version
    cell.set_state("PAUSED")
    assert _run(cell, needs_run=True).startswith("refused:predicate_false")
    assert _run(cell, expected=v).startswith("refused:version_changed")
    assert cell.conveyor == "STOPPED" and cell.effects == 0


def test_after_an_undone_pause_only_the_version_refuses():
    cell = _cell()
    v = cell.version
    cell.set_state("PAUSED")
    cell.set_state("RUN")                         # A-B-A: running again, version advanced twice
    assert _run(cell, expected=v).startswith("refused:version_changed")
    assert cell.conveyor == "STOPPED"
    res = _run(cell, needs_run=True)              # the interlock sees RUN and executes
    assert res == f"executed:RUN:{v + 2}" and cell.conveyor == "RUNNING"


def test_deadline_is_checked_in_the_scan():
    cell = _cell()
    assert _run(cell, expected=cell.version, deadline=1.0).startswith("refused:deadline")
    assert cell.conveyor == "STOPPED"


def test_openplc_program_implements_the_same_conveyor_semantics():
    st = (Path(__file__).resolve().parents[1] / "docker" / "openplc" / "cell_ctl.st").read_text()
    assert "conveyor AT %QW9" in st
    body = st.split("END_VAR", 2)[-1]
    assert "conveyor := 1;" in body.split("cmd_result := 1;")[0]      # set by an executed command
    assert "IF set_req = 0 THEN\n      conveyor := 0;" in body          # cleared by a pause


def test_version_is_not_reused_across_restarts_with_a_state_file(tmp_path):
    f = str(tmp_path / "cell_version.json")
    first = cell_controller.Cell(None, state_file=f, version_seed=10)
    first.set_state("PAUSED")
    first.set_state("RUN")
    used = first.version                                   # 12
    restarted = cell_controller.Cell(None, state_file=f)
    assert restarted.version == used + 1
    restarted.set_state("PAUSED")
    assert cell_controller.Cell(None, state_file=f).version == restarted.version + 1


def test_default_version_starts_at_the_boot_epoch():
    import time
    before = time.time_ns() // 1000
    cell = cell_controller.Cell(None)
    assert cell.version >= before                          # a restart starts above any earlier run
    v = cell.version
    cell.set_state("PAUSED")
    assert cell.version == v + 1
