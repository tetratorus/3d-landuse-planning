"""Golden tests: both descriptors validate and reproduce the outputs frozen from the unmodified upstream."""
from __future__ import annotations

from pathlib import Path

import pytest
from spine.golden import run_golden
from spine.transform import load_descriptor

REWIRE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("tid", ["ura.mp2019_conform", "gfa.allowable"])
def test_golden(tid):
    desc = load_descriptor(REWIRE / tid / "transform.yaml")
    results = run_golden(desc)
    assert results
    for r in results:
        assert r.passed, r.messages
