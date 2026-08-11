#!/usr/bin/env python3
"""Deprecated: use run_e1_baselines.py for symmetric HTTP evaluation."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
raise SystemExit(
    subprocess.call([sys.executable, str(ROOT / "run_e1_baselines.py"), *sys.argv[1:]])
)
