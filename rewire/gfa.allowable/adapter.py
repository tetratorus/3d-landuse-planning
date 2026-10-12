"""gfa.allowable: Silver plots + zoning + regulations (+ id_bridge) -> Gold allowable GFA.

Runs the UNMODIFIED upstream chain ``main.estimate_allowable_gfas`` (utils.py regulation logic) with
only its I/O rebound: SPARQL retrievers are served from Silver (via ../kg_codec.py) and the N-Quad
writer is replaced by a capture. No Blazegraph needed.
"""
from __future__ import annotations

import math

import kg_codec
import pandas as pd
import upstream_harness

NO_PROGRAMME = "unspecified"


def _iri_map(id_bridge: pd.DataFrame) -> pd.DataFrame:
    b = id_bridge[(id_bridge["source"] == kg_codec.IRI_SOURCE) & (id_bridge["hub"] == "plots")]
    return b[["source_id", "hub_id"]].rename(columns={"source_id": "plots", "hub_id": "plot_id"})


def flatten(result: pd.DataFrame, iri_to_id: dict[str, str]) -> dict[str, pd.DataFrame]:
    rows = []
    for iri, gfa in zip(result["plots"], result["gfa"]):
        for programme, value in (gfa if isinstance(gfa, dict) else {}).items():
            v = None if value is None or (isinstance(value, float) and math.isnan(value)) else float(value)
            rows.append((iri_to_id[iri], NO_PROGRAMME if programme is None else str(programme), v))
    by_prog = pd.DataFrame(rows, columns=["plot_id", "programme", "gfa_m2"])
    by_prog["gfa_m2"] = by_prog["gfa_m2"].astype(float)
    by_prog = by_prog.sort_values(["plot_id", "programme"], ignore_index=True)

    plot_ids = sorted(iri_to_id[i] for i in result["plots"])
    valid = by_prog.dropna(subset=["gfa_m2"])
    best = valid.sort_values(["gfa_m2", "programme"], ascending=[False, True]).drop_duplicates("plot_id")
    per_plot = pd.DataFrame({"plot_id": plot_ids}).merge(
        best.rename(columns={"programme": "max_gfa_programme", "gfa_m2": "max_gfa_m2"}), on="plot_id", how="left")
    per_plot["n_programmes"] = per_plot["plot_id"].map(by_prog.groupby("plot_id").size()).fillna(0).astype(int)
    per_plot = per_plot[["plot_id", "max_gfa_m2", "max_gfa_programme", "n_programmes"]]
    return {"allowable_gfa": per_plot, "allowable_gfa_by_programme": by_prog}


def run(inputs, params):
    bridge = inputs["id_bridge"]
    frames = kg_codec.silver_to_frames(inputs["plots"], inputs["zoning"], inputs["regulations"], bridge)
    m = _iri_map(bridge)
    iri_to_id = dict(zip(m["plots"], m["plot_id"]))
    scope = None
    if params.get("plot_ids"):
        id_to_iri = dict(zip(m["plot_id"], m["plots"]))
        missing = [p for p in params["plot_ids"] if p not in id_to_iri]
        if missing:
            raise ValueError(f"plot_ids not in silver: {missing[:5]}")
        scope = [id_to_iri[p] for p in params["plot_ids"]]
    result = upstream_harness.run_estimate_allowable_gfas(
        upstream_harness.offline_config(params.get("scenario", False)),
        kg_codec.retrievers_from_frames(frames), scope=scope, seed=int(params.get("seed", 0)))
    return flatten(result, iri_to_id)
