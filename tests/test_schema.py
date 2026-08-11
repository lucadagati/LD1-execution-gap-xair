from __future__ import annotations

import json
from pathlib import Path

import jsonschema


def test_ais_schema_validates_example():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "action-intent-v1.json").read_text())
    example = json.loads((root / "examples" / "manufacturing-stop-robot.json").read_text())
    jsonschema.validate(example, schema)
