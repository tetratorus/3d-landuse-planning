"""Snapshot the upstream knowledge graph into a Bronze extract, and optionally freeze a golden run.

Runs the UNMODIFIED upstream ``main.estimate_allowable_gfas`` against the upstream Blazegraph KG
(loaded with ``python main.py setup-reproducibility-blazegraph`` + the ``plots`` namespace, see
rewire/README.md). Every SPARQL retriever is wrapped so its exact return value is recorded; the
records become the Bronze source ``ura_3dlup_kg_extract`` that ``ura.mp2019_conform`` reads.
The final ``gfa_plots`` of the same unmodified run is written next to it (``_gfa_result.pkl``)
so it can be frozen as golden expected output.

    python rewire/bronze/extract_kg.py OUT_DIR [--scope plot_iris.txt] [--context-m 150]
    python rewire/bronze/extract_kg.py OUT_DIR --from-records   # re-export Bronze from _records.pkl

``--scope`` limits the GFA computation (at upstream's own scope hook) to the listed plot IRIs and
the extract to those plots plus every plot within ``--context-m`` metres (neighbours, roads).
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from upstream_harness import REPO, RETRIEVERS, import_main, run_estimate_allowable_gfas

# Not an upstream retriever: the MP2019 per-plot key, used for the stable spine hub id.
INC_CRC_QUERY = """
PREFIX ocgml:<http://www.theworldavatar.com/ontology/ontocitygml/citieskg/OntoCityGML.owl#>
SELECT ?plots ?inc_crc
WHERE { GRAPH <http://www.theworldavatar.com:83/citieskg/namespace/singaporeEPSG4326/sparql/cityobjectgenericattrib/> {
  ?attr ocgml:cityObjectId ?plots ; ocgml:attrName 'INC_CRC' ; ocgml:strVal ?inc_crc . } }
"""


REGULATION_RETRIEVERS = (
    "get_development_control_plans", "get_street_block_plans", "get_urban_design_guidelines",
    "get_landed_housing_areas", "get_road_categories", "get_height_control_plans",
)
# Upstream builds the plot frame in a chain: get_plots -> process_plots (science) -> drop 'zone' ->
# each enrichment retriever LEFT-merges extra KG columns on 'plots'. Bronze keeps get_plots as
# returned, and for each enrichment only the columns it adds, keyed by 'plots'.
PLOT_ENRICHMENTS = (
    "get_plot_properties", "get_neighbours", "get_plot_neighbour_types", "get_plot_allowed_programmes",
)


def _encode(series):
    """Parquet-safe encoding of upstream object columns; returns (series, kind)."""
    sample = series.dropna()
    sample = sample.iloc[0] if len(sample) else None
    if isinstance(sample, (set, frozenset)):
        return series.map(lambda v: sorted(v) if isinstance(v, (set, frozenset)) else None), "set"
    if isinstance(sample, list):
        return series.map(lambda v: list(v) if isinstance(v, list) else None), "list"
    return series, str(series.dtype)


def write_bronze(records: dict, keep: set | None, out_dir: Path) -> dict:
    """Write the recorded retriever outputs as Bronze (Geo)Parquet files plus _schema.json."""
    schema: dict[str, dict] = {}

    def put(name, df):
        df = df.reset_index(drop=True).copy()
        kinds = {}
        for c in df.columns:
            if c == "geometry":
                kinds[c] = "geometry"
                continue
            df[c], kinds[c] = _encode(df[c])
        if isinstance(df, gpd.GeoDataFrame) and "geometry" in df.columns:
            df.to_parquet(out_dir / f"{name}.parquet", index=False)
        else:
            pd.DataFrame(df).to_parquet(out_dir / f"{name}.parquet", index=False)
        schema[name] = {"columns": list(df.columns), "kinds": kinds, "rows": len(df),
                        "crs": str(df.crs) if isinstance(df, gpd.GeoDataFrame) and df.crs else None}

    in_keep = (lambda s: s.isin(keep)) if keep is not None else (lambda s: pd.Series(True, index=s.index))
    for name in REGULATION_RETRIEVERS:
        put(name, records[name])
    links = records["get_regulation_links"]
    put("get_regulation_links", links[in_keep(links["plots"])])
    plots = records["get_plots"]
    put("get_plots", plots[in_keep(plots["plots"])])
    prev = [c for c in plots.columns if c != "zone"]
    for name in PLOT_ENRICHMENTS:
        out = records[name]
        added = [c for c in out.columns if c not in prev]
        put(name, pd.DataFrame(out.loc[in_keep(out["plots"]), ["plots", *added]]))
        schema[name]["adds"] = added
        prev = list(out.columns)
    # Unfiltered: neighbour sets of kept plots reference plots outside the context ring.
    put("inc_crc", records["inc_crc"])
    (out_dir / "_schema.json").write_text(json.dumps(schema, indent=1))
    return schema


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--scope", type=Path, help="text file, one plot IRI per line")
    ap.add_argument("--context-m", type=float, default=150.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--from-records", action="store_true", help="only re-export Bronze from _records.pkl")
    a = ap.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    if a.from_records:
        with open(a.out_dir / "_records.pkl", "rb") as f:
            d = pickle.load(f)
        write_bronze(d["records"], d["keep"], a.out_dir)
        return

    upstream = import_main()
    import os
    os.chdir(REPO)
    config = upstream.load_config(str(REPO / "config.ini"))
    config.set("scenario", "scenario", "false")
    scope = None
    if a.scope:
        scope = [s.strip() for s in a.scope.read_text().splitlines() if s.strip()]

    records: dict[str, object] = {}
    timings: dict[str, float] = {}

    def recorder(name):
        fn = getattr(upstream, name)

        def wrapped(*args, **kwargs):
            t = time.time()
            out = fn(*args, **kwargs)
            timings[name] = round(time.time() - t, 1)
            records[name] = out.copy()
            print(f"[extract] {name}: {len(out)} rows, {timings[name]}s", flush=True)
            return out
        return wrapped

    t0 = time.time()
    gfa_plots = run_estimate_allowable_gfas(
        config, {n: recorder(n) for n in RETRIEVERS}, scope=scope, seed=a.seed)
    print(f"[extract] upstream run finished in {time.time() - t0:.0f}s", flush=True)

    keep = None
    if scope is not None:
        plots = records["get_plot_allowed_programmes"]
        core = plots[plots["plots"].isin(scope)]
        ring = core.geometry.buffer(a.context_m).unary_union
        keep = set(plots.loc[plots.intersects(ring), "plots"])
        keep |= set(records["get_plots"].loc[records["get_plots"].intersects(ring), "plots"])

    plots_endpoint = f'{config.get("endpoints", "in_endpoint")}/{config.get("namespaces", "plots")}/sparql'
    inc_crc = upstream.get_query_result(plots_endpoint, INC_CRC_QUERY)
    records["inc_crc"] = inc_crc
    print(f"[extract] inc_crc: {len(inc_crc)} rows", flush=True)

    with open(a.out_dir / "_records.pkl", "wb") as f:
        pickle.dump({"records": records, "keep": keep}, f)
    with open(a.out_dir / "_gfa_result.pkl", "wb") as f:
        pickle.dump(gfa_plots, f)
    write_bronze(records, keep, a.out_dir)
    (a.out_dir / "_extract.json").write_text(json.dumps({
        "scope": scope, "context_m": a.context_m, "seed": a.seed,
        "timings_s": timings, "plots_kept": None if keep is None else len(keep),
    }, indent=1))


if __name__ == "__main__":
    main()
