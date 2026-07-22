from __future__ import annotations

import json
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]


def test_worker_normalized_response_matches_public_json_schema() -> None:
    script = """
import { testSupport } from './worker/src/index.js';
const asset = testSupport.CATALOG.find((item) => item.symbol === 'NVDA');
const document = testSupport.normalizedDocument(
  [asset],
  [[['2026-01-02', 100], ['2026-01-05', 110]]],
);
process.stdout.write(JSON.stringify(document));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    document = json.loads(completed.stdout)
    schema = json.loads(
        (ROOT / "schemas/kelly-price-series.schema.json").read_text(encoding="utf-8")
    )
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: error.json_path,
    )
    assert errors == []
    assert document["metadata"][0]["returnBasis"] == "total_return_approximation"
    assert document["metadata"][0]["priceField"] == "adjusted_close"
    assert document["source"]["provider"] == "yahoo_finance"
    assert document["source"]["priceField"] == "adjusted_close"
    assert document["source"]["priceFieldBySymbol"] == {"NVDA": "adjusted_close"}
