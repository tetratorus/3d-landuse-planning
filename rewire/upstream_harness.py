"""Run the UNMODIFIED upstream ``main.estimate_allowable_gfas`` with its I/O swapped out.

``estimate_allowable_gfas`` is the upstream orchestration of the GFA science: it calls SPARQL
retrievers (``retrievers.py``), the regulation/geometry science (``utils.py``) and finally writes
N-Quads (``instantiators.instantiate_allowed_gfa``). This harness keeps that function and every
science call untouched and only rebinds the I/O names in the ``main`` module namespace
(Delft-FEWS General Adapter style): retrievers can be served from Silver tables instead of
Blazegraph, and the final N-Quad writer is replaced by a capture of the result DataFrame.
"""
from __future__ import annotations

import configparser
import contextlib
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]

# Every SPARQL-backed name that estimate_allowable_gfas() resolves from main's globals.
RETRIEVERS = (
    "get_development_control_plans",
    "get_street_block_plans",
    "get_urban_design_guidelines",
    "get_landed_housing_areas",
    "get_road_categories",
    "get_regulation_links",
    "get_height_control_plans",
    "get_plots",
    "get_plot_properties",
    "get_neighbours",
    "get_plot_neighbour_types",
    "get_plot_allowed_programmes",
)


def import_main():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import main  # upstream module, unmodified

    return main


def offline_config(scenario: bool = False) -> configparser.ConfigParser:
    """Config with only the keys estimate_allowable_gfas reads. Endpoints are never contacted
    because every retriever is rebound; paths are never written because the writer is captured."""
    c = configparser.ConfigParser()
    c.read_dict({
        "paths": {"out_dir": "unused", "scenario_dir": "unused"},
        "namespaces": {"plots": "plots"},
        "endpoints": {"in_endpoint": "offline://", "out_endpoint": "offline://",
                      "scenario_endpoint": "offline://"},
        "scenario": {"scenario": "true" if scenario else "false"},
    })
    return c


@contextlib.contextmanager
def _rebound(module, names: dict[str, Callable]):
    saved = {k: getattr(module, k) for k in names}
    try:
        for k, v in names.items():
            setattr(module, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


def run_estimate_allowable_gfas(config, retrievers: dict[str, Callable] | None = None,
                                scope: Iterable[str] | None = None, seed: int = 0):
    """Call upstream ``main.estimate_allowable_gfas(config)`` and return its final ``gfa_plots``.

    :param retrievers: replacements for names in RETRIEVERS (missing ones hit the real KG).
    :param scope: optional plot IRIs. Applied at the upstream's own documented scope hook
        ("Here you can define the scope of gfa calculation") i.e. right before
        ``set_plot_edge_setbacks``; all context steps before it still see every plot.
    :param seed: numpy global seed; upstream ``assign_sbp_gpr`` calls ``DataFrame.sample()``
        without a random_state.
    """
    main = import_main()
    captured = {}
    names = dict(retrievers or {})
    unknown = set(names) - set(RETRIEVERS)
    if unknown:
        raise KeyError(f"not an upstream retriever: {sorted(unknown)}")

    original_setbacks = main.set_plot_edge_setbacks
    scope_set = None if scope is None else set(scope)

    def scoped_setbacks(gfa_plots, *args, **kwargs):
        if scope_set is not None:
            gfa_plots = gfa_plots[gfa_plots["plots"].isin(scope_set)].copy()
        return original_setbacks(gfa_plots, *args, **kwargs)

    def capture_writer(df, out_dir, out_endpoint=None):
        captured["gfa_plots"] = df

    names["set_plot_edge_setbacks"] = scoped_setbacks
    names["instantiate_allowed_gfa"] = capture_writer
    with _rebound(main, names):
        np.random.seed(seed)
        main.estimate_allowable_gfas(config)
    return captured["gfa_plots"]
