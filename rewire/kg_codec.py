"""Lossless mapping: Bronze KG extract <-> Silver tables <-> the frames upstream retrievers return.

Shared by ``ura.mp2019_conform`` (Bronze -> Silver) and ``gfa.allowable`` (Silver -> upstream
retriever frames). Upstream works in EPSG:3857 and keys plots by TWA CityObject IRIs; Silver is
EPSG:3414 keyed by the MP2019 hub id ``plot_id`` with the IRI kept in ``id_bridge``.
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

UPSTREAM_CRS = "EPSG:3857"
SILVER_CRS = "EPSG:3414"
IRI_SOURCE = "twa_citieskg_plot"
MP19_SOURCE = "ura_mp2019_inc_crc"
SCHEMA = json.loads((Path(__file__).with_name("upstream_schema.json")).read_text())

REG_TYPES = {
    "get_development_control_plans": "DevelopmentControlPlan",
    "get_street_block_plans": "StreetBlockPlan",
    "get_urban_design_guidelines": "UrbanDesignGuideline",
    "get_landed_housing_areas": "LandedHousingArea",
    "get_height_control_plans": "HeightControlPlan",
    "get_road_categories": "RoadCategory",
}
ENRICHMENTS = ("get_plot_properties", "get_neighbours", "get_plot_neighbour_types",
               "get_plot_allowed_programmes")


def plot_id_from_inc_crc(inc_crc: pd.Series) -> pd.Series:
    return "mp19-" + inc_crc.str.lower()


def assign_plot_ids(plots: gpd.GeoDataFrame, crc: pd.DataFrame) -> pd.Series:
    """Hub id per KG plot. The KG splits a few MP2019 features into several CityObjects (slivers);
    the largest part keeps ``mp19-<inc_crc>``, the others get ``-p2``, ``-p3``... by area, then IRI."""
    m = pd.DataFrame({"plots": plots["plots"].values, "area": plots.geometry.area.values})
    m["inc_crc"] = m["plots"].map(dict(zip(crc["plots"], crc["inc_crc"])))
    if m["inc_crc"].isna().any():
        raise ValueError(f"{int(m['inc_crc'].isna().sum())} plots have no MP2019 INC_CRC")
    m = m.sort_values(["inc_crc", "area", "plots"], ascending=[True, False, True])
    rank = m.groupby("inc_crc").cumcount()
    m["plot_id"] = plot_id_from_inc_crc(m["inc_crc"]) + np.where(rank > 0, "-p" + (rank + 1).astype(str), "")
    return m.set_index("plots").loc[plots["plots"], "plot_id"].reset_index(drop=True)


def _decode(series: pd.Series, kind: str) -> pd.Series:
    if kind == "set":
        return series.map(lambda v: set(v) if v is not None and not _isna(v) else np.nan)
    if kind == "list":
        return series.map(lambda v: list(v) if v is not None and not _isna(v) else np.nan)
    return series


def _isna(v) -> bool:
    return not isinstance(v, (list, np.ndarray, set)) and pd.isna(v)


def _jsonable(v):
    if isinstance(v, (set, frozenset)):
        return {"__set__": sorted(v)}
    if isinstance(v, np.ndarray):
        return [_jsonable(x) for x in v.tolist()]
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return None if np.isnan(v) else float(v)
    return v


def _unjson(v):
    if isinstance(v, dict) and "__set__" in v:
        return set(v["__set__"])
    return v


def _kind(col: str) -> str:
    return next(SCHEMA[n]["kinds"][col] for n in ENRICHMENTS if col in SCHEMA[n]["kinds"])


def _json_cell(v):
    """Set/list cells -> JSON text (sets sorted) so Silver is plain, deterministic Parquet."""
    return None if _isna(v) else json.dumps(_jsonable(sorted(v) if isinstance(v, (set, frozenset)) else v))


def _unjson_cell(v):
    return json.loads(v) if isinstance(v, str) else v


def read_bronze(d: Path) -> dict[str, pd.DataFrame]:
    """Read a Bronze KG extract back into exactly the frames the upstream retrievers returned."""
    schema = json.loads((Path(d) / "_schema.json").read_text())
    out = {}
    for name, meta in schema.items():
        f = Path(d) / f"{name}.parquet"
        df = gpd.read_parquet(f) if "geometry" in meta["columns"] else pd.read_parquet(f)
        for c, kind in meta["kinds"].items():
            if c != "geometry":
                df[c] = _decode(df[c], kind)
        out[name] = df[meta["columns"]]
    return out


def bronze_to_silver(b: dict[str, pd.DataFrame], version: str) -> dict[str, pd.DataFrame]:
    crc = b["inc_crc"].drop_duplicates("plots")
    base = b["get_plots"].reset_index(drop=True)
    pid = assign_plot_ids(base, crc)
    iri_to_id = dict(zip(base["plots"], pid))

    enriched = base[["plots"]].copy()
    for name in ENRICHMENTS:
        enriched = enriched.merge(b[name], how="left", on="plots")
    enriched["neighbour"] = enriched["neighbour"].map(
        lambda s: {iri_to_id.get(n, n) for n in s} if isinstance(s, set) else s)
    extra_cols = [c for c in enriched.columns if c not in ("plots", "zone")]
    for c in extra_cols:
        if _kind(c) in ("set", "list"):
            enriched[c] = enriched[c].map(_json_cell)

    geom = base.geometry.to_crs(SILVER_CRS)
    plots = gpd.GeoDataFrame({"plot_id": pid.values, "area_m2": geom.area.values,
                              "source_version": version}, geometry=geom.values, crs=SILVER_CRS)
    for c in extra_cols:
        plots[c] = enriched[c].values
    plots["kg_row"] = np.arange(len(plots))
    plots["kg_enriched"] = base["plots"].isin(b["get_plot_properties"]["plots"]).values

    zoning = pd.DataFrame({
        "plot_id": pid.values,
        "zone_type": enriched["zone"].fillna(base["zone"]).values,
        "gross_plot_ratio": base["gpr"].values,
        "lu_desc": base["zone"].values,
        "source_version": version,
    })

    catalogue = {}
    for name, reg_type in REG_TYPES.items():
        df = b[name]
        key = "road_reg" if name == "get_road_categories" else "reg"
        for i, row in enumerate(df.itertuples(index=False)):
            rec = row._asdict()
            g = rec.pop("geometry", None)
            entry = {"reg_type": reg_type, "retriever": name, "row": i,
                     "attributes": {k: _jsonable(v) for k, v in rec.items()},
                     "geometry_wkb": None if g is None else shapely.to_wkb(
                         gpd.GeoSeries([g], crs=df.crs).to_crs(SILVER_CRS).iloc[0], hex=True)}
            catalogue.setdefault(rec[key], []).append(entry)
    dcp_roads = {r: list(v) for r, v in zip(b["get_development_control_plans"]["reg"],
                                            b["get_development_control_plans"]["road_categories"])}
    links = b["get_regulation_links"].reset_index(drop=True)
    links["link_row"] = np.arange(len(links))
    by_plot = {p: g for p, g in links.groupby("plots", sort=False)}
    regs_json = []
    emitted: set = set()

    def content(reg):
        if reg in emitted:
            return None
        emitted.add(reg)
        return catalogue.get(reg, [])

    for iri in base["plots"]:
        g = by_plot.get(iri)
        applicable = []
        if g is not None:
            for r in g.itertuples(index=False):
                applicable.append({"reg_id": r.reg, "reg_type": r.reg_type, "link_row": int(r.link_row),
                                   "content": content(r.reg)})
                for rr in dcp_roads.get(r.reg, []) if r.reg_type == "DevelopmentControlPlan" else []:
                    applicable.append({"reg_id": rr, "reg_type": "RoadCategory", "link_row": None,
                                       "content": content(rr)})
        regs_json.append(json.dumps(applicable))
    regulations = pd.DataFrame({"plot_id": pid.values, "applicable_regulations_json": regs_json,
                                "source_version": version})

    bridge = pd.concat([
        pd.DataFrame({"source": IRI_SOURCE, "source_id": base["plots"].values, "hub": "plot",
                      "hub_id": pid.values, "match_method": "exact", "confidence": 1.0,
                      "source_version": version}),
        pd.DataFrame({"source": MP19_SOURCE, "source_id": base["plots"].map(
                          dict(zip(crc["plots"], crc["inc_crc"]))).values, "hub": "plot",
                      "hub_id": pid.values, "match_method": "exact", "confidence": 1.0,
                      "source_version": version})[~pid.str.contains("-p", regex=False).values],
    ], ignore_index=True)
    return {"plots": plots, "zoning": zoning, "regulations": regulations, "id_bridge": bridge}


def silver_to_frames(plots: gpd.GeoDataFrame, zoning: pd.DataFrame, regulations: pd.DataFrame,
                     id_bridge: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Rebuild every upstream retriever's return value from Silver (inverse of bronze_to_silver)."""
    br = id_bridge[(id_bridge["source"] == IRI_SOURCE) & (id_bridge["hub"] == "plot")]
    id_to_iri = dict(zip(br["hub_id"], br["source_id"]))
    plots = plots.sort_values("kg_row") if "kg_row" in plots.columns else plots
    p = plots.merge(zoning, on="plot_id", how="left", suffixes=("", "_z"))
    iri = p["plot_id"].map(id_to_iri)
    if iri.isna().any():
        raise ValueError(f"{int(iri.isna().sum())} plots have no {IRI_SOURCE} id_bridge row")
    geom = gpd.GeoSeries(p.geometry.values, crs=plots.crs).to_crs(UPSTREAM_CRS)
    frames = {}
    gp = gpd.GeoDataFrame({"plots": iri.values, "zone": p["lu_desc"].values,
                           "gpr": p["gross_plot_ratio"].astype(float).values}, geometry=geom.values,
                          crs=UPSTREAM_CRS)
    gp["plot_area"] = gp.area
    frames["get_plots"] = gp[SCHEMA["get_plots"]["columns"]]
    enr = p["kg_enriched"].astype(bool).values if "kg_enriched" in p.columns else np.ones(len(p), bool)
    for name in ENRICHMENTS:
        cols = SCHEMA[name]["adds"]
        df = pd.DataFrame({"plots": iri.values[enr]})
        for c in cols:
            src = p["zone_type"] if c == "zone" else p[c]
            kind = SCHEMA[name]["kinds"][c]
            if kind in ("set", "list"):
                src = src.map(_unjson_cell)
            if c == "neighbour":
                src = src.map(lambda v: {id_to_iri.get(n, n) for n in v} if not _isna(v) else np.nan)
            else:
                src = _decode(src, kind)
            df[c] = src.values[enr] if kind in ("set", "list", "object") else src[enr].astype(kind).values
        frames[name] = df

    rows = {name: {} for name in REG_TYPES}
    link_rows = []
    referenced, defined = set(), set()
    for pid_, js in zip(regulations["plot_id"], regulations["applicable_regulations_json"]):
        for a in json.loads(js):
            if a["link_row"] is not None:
                link_rows.append((a["link_row"], a["reg_id"], id_to_iri.get(pid_, pid_), a["reg_type"]))
            if a["content"] is None:
                referenced.add(a["reg_id"])
            else:
                defined.add(a["reg_id"])
            for e in a["content"] or []:
                rows[e["retriever"]][e["row"]] = e
    if referenced - defined:
        raise ValueError(f"{len(referenced - defined)} regulations referenced but not defined: Silver "
                         "regulations stores each regulation's content once, read all rows of a source_version")
    for name in REG_TYPES:
        meta = SCHEMA[name]
        ents = [rows[name][k] for k in sorted(rows[name])]
        data = {c: [_unjson(e["attributes"].get(c)) for e in ents] for c in meta["columns"] if c != "geometry"}
        df = pd.DataFrame(data, columns=[c for c in meta["columns"] if c != "geometry"])
        for c, kind in meta["kinds"].items():
            if c != "geometry" and kind not in ("set", "list", "object"):
                df[c] = pd.to_numeric(df[c]).astype(kind) if len(df) else df[c].astype(kind)
        if "geometry" in meta["columns"]:
            g = gpd.GeoSeries([shapely.from_wkb(e["geometry_wkb"]) for e in ents], crs=SILVER_CRS)
            df = gpd.GeoDataFrame(df, geometry=g.to_crs(UPSTREAM_CRS).values, crs=UPSTREAM_CRS)
        frames[name] = df[meta["columns"]]
    link_rows.sort()
    frames["get_regulation_links"] = pd.DataFrame([r[1:] for r in link_rows],
                                                  columns=SCHEMA["get_regulation_links"]["columns"])
    return frames


def retrievers_from_frames(frames: dict[str, pd.DataFrame]) -> dict:
    """Drop-in replacements for upstream's SPARQL retrievers, serving pre-built frames.

    Endpoint-only retrievers return a copy of their frame. The plot enrichments mirror upstream's
    LEFT merge on 'plots' onto the frame that upstream science passes in (after process_plots).
    """
    def static(name):
        return lambda *_a, **_k: frames[name].copy()

    def enrich(name):
        return lambda plots, *_a, **_k: plots.merge(frames[name], how="left", on="plots")

    out = {name: static(name) for name in (*REG_TYPES, "get_regulation_links", "get_plots")}
    out.update({name: enrich(name) for name in ENRICHMENTS})
    return out
