"""ura.mp2019_conform: Bronze (3DLUP KG extract + LTA road network) -> Silver.

Thin adapter: all mapping logic lives in ../kg_codec.py (shared with gfa.allowable so the two stay
inverse of each other). No upstream science runs here.
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import kg_codec
import pandas as pd

ROAD_SOURCE = "lta_road_network"


def conform_roads(raw: gpd.GeoDataFrame, version: str):
    raw = raw.to_crs(kg_codec.SILVER_CRS)
    road_id = "lta-rd-" + raw["UNIQUE_ID"].astype(str)
    roads = gpd.GeoDataFrame({
        "road_id": road_id.values,
        "road_class": raw["RD_TYP_CD"].values,
        "name": raw["RD_NAME"].values,
        "level_of_road": raw["LVL_OF_RD"].values,
        "source_version": version,
    }, geometry=raw.geometry.values, crs=kg_codec.SILVER_CRS)
    bridge = pd.DataFrame({
        "source": ROAD_SOURCE, "source_id": raw["UNIQUE_ID"].astype(str).values, "hub": "roads",
        "hub_id": road_id.values, "match_method": "exact_key", "confidence": 1.0, "source_version": version,
    })
    return roads, bridge


def _read_roads(d: Path) -> gpd.GeoDataFrame:
    for pattern in ("*.parquet", "*.shp", "*.geojson"):
        files = sorted(d.glob(pattern))
        if files:
            f = files[0]
            return gpd.read_parquet(f) if f.suffix == ".parquet" else gpd.read_file(f)
    raise FileNotFoundError(f"no road network file in {d}")


def run(inputs, params):
    kg_dir, roads_dir = Path(inputs["kg"]), Path(inputs["roads"])
    silver = kg_codec.bronze_to_silver(kg_codec.read_bronze(kg_dir), kg_dir.name)
    roads, road_bridge = conform_roads(_read_roads(roads_dir), roads_dir.name)
    silver["roads"] = roads
    own = pd.concat([silver["id_bridge"], road_bridge], ignore_index=True)
    silver["id_bridge"] = pd.concat([_foreign_bridge(inputs.get("hubs")), own], ignore_index=True)
    return silver


OWN_SOURCES = {kg_codec.IRI_SOURCE, kg_codec.MP19_SOURCE, ROAD_SOURCE}


def _foreign_bridge(hubs_dir) -> pd.DataFrame:
    """id_bridge is shared and silver_write replaces whole tables: re-emit other sources' rows from the snapshot."""
    f = Path(hubs_dir) / "id_bridge.parquet" if hubs_dir is not None else None
    if f is None or not f.exists():
        return pd.DataFrame()
    snap = pd.read_parquet(f)
    return snap[~snap["source"].isin(OWN_SOURCES)].reset_index(drop=True)
