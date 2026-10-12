"""Bronze -> Silver -> upstream retriever frames must reproduce the upstream frames exactly."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import shapely

REWIRE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REWIRE))

import kg_codec
from spine.spec import silver_table
from spine.validate import check_silver

KG = REWIRE / "ura.mp2019_conform" / "golden" / "sg_44_plots" / "inputs" / "kg"


@pytest.fixture(scope="module")
def roundtrip():
    bronze = kg_codec.read_bronze(KG)
    silver = kg_codec.bronze_to_silver(bronze, "test")
    frames = kg_codec.silver_to_frames(silver["plots"], silver["zoning"], silver["regulations"], silver["id_bridge"])
    return bronze, silver, frames


def assert_same(got: pd.DataFrame, want: pd.DataFrame):
    got, want = got.reset_index(drop=True), want.reset_index(drop=True)
    assert list(got.columns) == list(want.columns)
    assert len(got) == len(want)
    for c in want.columns:
        g, w = got[c], want[c]
        if c == "geometry":
            d = [float(shapely.hausdorff_distance(a, b)) for a, b in zip(g, w)]
            assert max(d, default=0.0) < 1e-3, c
        elif pd.api.types.is_numeric_dtype(w):
            assert g.dtype == w.dtype, c
            assert np.allclose(g.astype(float), w.astype(float), rtol=1e-9, atol=1e-6, equal_nan=True), c
        else:
            assert all((kg_codec._isna(a) and kg_codec._isna(b)) or a == b for a, b in zip(g, w)), c


def test_silver_tables_validate(roundtrip):
    _, silver, _ = roundtrip
    for name, df in silver.items():
        check_silver(name, df)


def test_hub_ids_from_inc_crc(roundtrip):
    _, silver, _ = roundtrip
    pid = silver["plots"]["plot_id"]
    assert pid.is_unique
    assert pid.str.match(r"^mp19-[0-9a-f]{16}(-p\d+)?$").all()
    mp19 = silver["id_bridge"].query("source == @kg_codec.MP19_SOURCE")
    assert (("mp19-" + mp19["source_id"].str.lower()) == mp19["hub_id"]).all()


def test_id_bridge_uses_documented_vocab(roundtrip):
    _, silver, _ = roundtrip
    cols = silver_table("id_bridge")["columns"]
    assert set(silver["id_bridge"]["hub"]) <= {v.strip() for v in cols["hub"]["doc"].split("|")}
    assert set(silver["id_bridge"]["match_method"]) <= {v.strip() for v in cols["match_method"]["doc"].split("|")}


@pytest.mark.parametrize("name", ["get_plots", *kg_codec.ENRICHMENTS])
def test_plot_frames_roundtrip(roundtrip, name):
    bronze, _, frames = roundtrip
    want = bronze[name]
    got = frames[name].set_index("plots").loc[want["plots"]].reset_index()
    assert_same(got, want)


@pytest.mark.parametrize("name", [*kg_codec.REG_TYPES, "get_regulation_links"])
def test_regulation_frames_roundtrip(roundtrip, name):
    bronze, silver, frames = roundtrip
    if name == "get_regulation_links":
        assert_same(frames[name], bronze[name])
        return
    used = set()
    for js in silver["regulations"]["applicable_regulations_json"]:
        for a in json.loads(js):
            used |= {e["row"] for e in a["content"] or [] if e["retriever"] == name}
    assert_same(frames[name], bronze[name].iloc[sorted(used)])


def test_subset_read_is_rejected(roundtrip):
    _, silver, _ = roundtrip
    regs = silver["regulations"].iloc[1:]
    with pytest.raises(ValueError, match="referenced but not defined"):
        kg_codec.silver_to_frames(silver["plots"], silver["zoning"], regs, silver["id_bridge"])
