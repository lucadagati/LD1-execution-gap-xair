"""Provenance manifests: file digests, environment record, and the run/retroactive distinction."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "make_manifest.py"


def _run(*args: str) -> None:
    subprocess.run([sys.executable, str(SCRIPT), *args], check=True, capture_output=True)


def test_retroactive_manifest_records_digests_and_what_is_missing(tmp_path):
    (tmp_path / "environment.txt").write_text('{"loadavg": ["1", "1", "1"]}\n')
    (tmp_path / "e1.csv").write_text("a,b\n1,2\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "e2.csv").write_text("x\n")
    _run("--retro", str(tmp_path), "--command", "./scripts/run.sh", "--seed", "42", "--flat")
    doc = json.loads((tmp_path / "MANIFEST.json").read_text())
    assert doc["kind"] == "retroactive" and doc["commands"] == ["./scripts/run.sh"] and doc["seeds"] == ["42"]
    paths = {f["path"]: f for f in doc["files"]}
    assert set(paths) == {"e1.csv", "environment.txt"}              # --flat: sub-directories excluded
    assert paths["e1.csv"]["sha256"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert "git commit" in doc["not_recorded_at_run_time"]
    assert doc["environment_recorded_at_run_time"]["environment.txt"] == ['{"loadavg": ["1", "1", "1"]}']


def test_run_manifest_records_git_runtime_and_host(tmp_path):
    (tmp_path / "out.csv").write_text("k\n1\n")
    _run("--run", str(tmp_path), "--command", "python x.py --seed 7", "--seed", "7")
    doc = json.loads((tmp_path / "MANIFEST.json").read_text())
    assert doc["kind"] == "run"
    assert doc["git"]["commit"] is None or len(doc["git"]["commit"]) == 40
    assert doc["runtime"]["python"] and "requirements_lock_sha256" in doc["runtime"]
    assert doc["host"]["machine"]
    assert [f["path"] for f in doc["files"]] == ["out.csv"]           # the manifest never lists itself


def test_check_reports_a_modified_file(tmp_path):
    (tmp_path / "e1.csv").write_text("a\n1\n")
    (tmp_path / "README.md").write_text("docs\n")
    _run("--retro", str(tmp_path), "--command", "x")
    assert "README.md" not in {f["path"] for f in json.loads((tmp_path / "MANIFEST.json").read_text())["files"]}
    ok = subprocess.run([sys.executable, str(SCRIPT), "--check", str(tmp_path)], capture_output=True)
    assert ok.returncode == 0
    (tmp_path / "e1.csv").write_text("a\n2\n")
    bad = subprocess.run([sys.executable, str(SCRIPT), "--check", str(tmp_path)], capture_output=True, text=True)
    assert bad.returncode == 1 and "MISMATCH e1.csv" in bad.stderr
