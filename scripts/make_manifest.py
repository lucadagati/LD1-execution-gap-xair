#!/usr/bin/env python3
"""Machine-readable provenance manifest (MANIFEST.json) for a campaign directory.

Two uses:

* after a run (``--run``): records what can be observed now -- git commit and
  working-tree state, runtime and dependency versions, host, container images
  (id and registry digest), the command line and seeds supplied by the caller,
  and a SHA-256 for every output file;
* ``--check [DIR]``: verifies every MANIFEST.json under DIR (default data/):
  each listed file must exist with the recorded SHA-256; exits 1 otherwise;
* for frozen data produced before manifests existed (``--retro``): records the
  file digests and the run-time ``environment*.txt`` content, together with the
  producing command and seeds as documented in the repository's scripts, and
  marks explicitly what was not recorded at run time (commit, image digests).

Usage:
  python scripts/make_manifest.py --run DIR --command "..." [--seed N] [--image redis:7-alpine ...]
  python scripts/make_manifest.py --retro DIR --command "..." [--seed N] [--note "..."]
  python scripts/make_manifest.py --check [DIR]

Markdown files (READMEs) are documentation that can be corrected after a run,
so manifests do not list them.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "MANIFEST.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def files(d: Path, flat: bool = False) -> list[dict]:
    out = []
    for p in sorted(d.glob("*") if flat else d.rglob("*")):
        if p.is_file() and p.name != MANIFEST and p.suffix != ".md":
            out.append({"path": str(p.relative_to(d)), "bytes": p.stat().st_size, "sha256": sha256(p)})
    return out


def environment(d: Path) -> dict:
    env = {}
    for p in sorted(d.glob("environment*.txt")):
        env[p.name] = p.read_text(errors="replace").splitlines()
    return env


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout
    except Exception:
        return None


def git_state() -> dict:
    head = (_git("rev-parse", "HEAD") or "").strip() or None
    status = _git("status", "--porcelain", "--untracked-files=normal") or ""
    diff = _git("diff", "HEAD", "--binary") or ""
    return {"commit": head, "dirty": bool(status.strip()),
            # identifies the exact uncommitted tree the run used (tracked files)
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest() if status.strip() else None,
            "changed_paths": [ln[3:] for ln in status.splitlines()]}


def image(ref: str) -> dict:
    try:
        out = subprocess.run(["docker", "image", "inspect", ref, "--format",
                              "{{json .Id}}|{{json .RepoDigests}}|{{json .Created}}"],
                             capture_output=True, text=True, check=True).stdout.strip()
        iid, digests, created = (json.loads(x) for x in out.split("|", 2))
        return {"ref": ref, "id": iid, "repo_digests": digests, "created": created}
    except Exception as exc:
        return {"ref": ref, "error": type(exc).__name__}


def host() -> dict:
    cpu = ""
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {"platform": platform.platform(), "machine": platform.machine(), "cpu": cpu,
            "cpus_visible": os.cpu_count(),
            "loadavg": open("/proc/loadavg").read().split()[:3] if os.path.exists("/proc/loadavg") else None}


def runtime_versions() -> dict:
    sys.path.insert(0, str(ROOT))
    try:
        from xair import __version__ as xair_version
    except Exception:
        xair_version = None
    deps = {}
    for name in ("fastapi", "uvicorn", "redis", "jsonschema", "pydantic", "asyncua", "pymodbus", "httpx"):
        try:
            deps[name] = md.version(name)
        except md.PackageNotFoundError:
            pass
    lock = ROOT / "requirements.lock"
    return {"xair": xair_version, "python": platform.python_version(), "dependencies": deps,
            "requirements_lock_sha256": sha256(lock) if lock.exists() else None}


def check(root: Path) -> int:
    """Verify every manifest under ``root``; print each mismatch and return the number of bad files."""
    bad = n = 0
    for m in sorted(root.rglob(MANIFEST)):
        for f in json.loads(m.read_text())["files"]:
            n += 1
            p = m.parent / f["path"]
            if not p.is_file() or sha256(p) != f["sha256"]:
                bad += 1
                print(f"MISMATCH {p.relative_to(root)}", file=sys.stderr)
    print(f"{n - bad}/{n} files match their manifests under {root}")
    return bad


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--check":
        return 1 if check(Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT / "data") else 0
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", type=Path, help="directory just produced by a run")
    mode.add_argument("--retro", type=Path, help="frozen directory produced before manifests existed")
    ap.add_argument("--command", action="append", default=[], help="producing command (repeatable)")
    ap.add_argument("--seed", action="append", default=[], help="seed(s) used (repeatable)")
    ap.add_argument("--image", action="append", default=[], help="container image reference used (repeatable)")
    ap.add_argument("--started", default=None, help="run start time (ISO 8601), if known")
    ap.add_argument("--note", action="append", default=[])
    ap.add_argument("--code", action="append", default=[], type=Path,
                    help="code file the run executed (repeatable): its SHA-256 is recorded")
    ap.add_argument("--flat", action="store_true", help="only the directory's own files (sub-directories have their own manifest)")
    args = ap.parse_args()
    d = (args.run or args.retro).resolve()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc = {
        "manifest_version": 1,
        "directory": str(d.relative_to(ROOT)) if d.is_relative_to(ROOT) else str(d),
        "kind": "run" if args.run else "retroactive",
        "commands": args.command,
        "seeds": args.seed,
        "files": files(d, args.flat),
        "environment_recorded_at_run_time": environment(d),
        "notes": args.note,
    }
    if args.code:
        doc["code_files"] = [{"path": str(c), "sha256": sha256(ROOT / c)} for c in args.code]
    if args.run:
        doc.update({"started": args.started, "manifest_written": now, "git": git_state(),
                    "runtime": runtime_versions(), "host": host(), "images": [image(r) for r in args.image]})
    else:
        doc.update({"manifest_written": now,
                    "not_recorded_at_run_time": ["git commit", "dependency versions beyond environment.txt",
                                                 "container image digests"],
                    "published_snapshot": "github.com/ProSoDiAC/LD1-execution-gap-xair (single-commit snapshot)"})
    (d / MANIFEST).write_text(json.dumps(doc, indent=2) + "\n")
    print(f"{d / MANIFEST}: {len(doc['files'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
