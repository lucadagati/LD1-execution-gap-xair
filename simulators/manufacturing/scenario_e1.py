"""
DEPRECATED for paper metrics — use Unity ManufacturingTestOrchestrator + HTTP stack.

This module remains for unit reference only. Paper evaluation must use:
  - AdaptiX-Quest/TestResults/*.json (Unity Editor), or
  - experiments/run_e1_http_stack.py (full HTTP adapter path, no in-process XAIR).
"""

from __future__ import annotations

from simulators.manufacturing.scenario_e1 import ManufacturingScenario, run_xair  # noqa: F401
