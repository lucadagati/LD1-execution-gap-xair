#!/usr/bin/env python3
"""Resolve a paper experiment identifier (E1 ... E9 and their variants, e.g. E4-ABA, E5-scale) through experiments.yaml.

  python scripts/reproduce.py --list
  python scripts/reproduce.py E8            # artifact id, scripts, data files (with sizes)

It prints what to run and which data files hold the results; it does not start a campaign.
The campaign scripts under scripts/ (run_distributed.sh, run_physical.sh, ...) take their own options.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "execution-gap"


def load() -> dict[str, dict]:
    """Parse the flat one-line-per-entry YAML subset used by experiments.yaml (no dependency on PyYAML)."""
    try:
        import yaml  # type: ignore
        return yaml.safe_load((ROOT / "experiments.yaml").read_text(encoding="utf-8"))
    except ImportError:
        import json
        import re
        out: dict[str, dict] = {}
        for line in (ROOT / "experiments.yaml").read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z0-9-]+):\s*(\{.*\})\s*$", line)
            if m:
                body = re.sub(r"(\w+):", r'"\1":', m.group(2))
                body = re.sub(r'"artifact":\s*([A-Za-z0-9-]+)', r'"artifact": "\1"', body)
                body = re.sub(r"\[([^\]\"]*)\]", lambda g: "[" + ", ".join('"%s"' % x.strip() for x in g.group(1).split(",")) + "]", body)
                out[m.group(1)] = json.loads(body)
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("id", nargs="?")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    table = load()
    if args.list or not args.id:
        for k, v in table.items():
            print(f"{k:<12} -> {v['artifact']}")
        return 0
    entry = table.get(args.id)
    if entry is None:
        print(f"unknown identifier {args.id}; try --list", file=sys.stderr)
        return 1
    print(f"{args.id}: artifact {entry['artifact']}")
    print("scripts:")
    for s in entry["scripts"]:
        print(f"  {s}{'' if (ROOT / s).exists() else '  (missing)'}")
    print("data:")
    seen: set[Path] = set()
    for pattern in entry["data"]:
        for f in sorted(DATA.glob(pattern)):
            if f.is_file() and f not in seen:
                seen.add(f)
                print(f"  {f.relative_to(DATA)}  ({f.stat().st_size} bytes)")
    if not seen:
        print("  (no files match)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
