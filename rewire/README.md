# rewire/ — 3D Land Use Planning on the sg-urban-spine

This directory plugs 3D Land Use Planning (3DLUP) into
[sg-urban-spine](https://github.com/tetratorus/sg-urban-spine) without changing its science.
It adds two transforms from the spine's transform-id table (`spec/LAYERS.md`):

| id | kind | what it does |
|---|---|---|
| `ura.mp2019_conform` | conformer | Bronze 3DLUP planning KG extract + LTA roads → Silver `plots`, `zoning`, `regulations`, `roads`, `id_bridge` |
| `gfa.allowable` | transform | Silver → Gold `allowable_gfa` (per `plot_id`), `allowable_gfa_by_programme`, `allowable_gfa_uncomputed`, using the **unmodified** upstream regulation logic |

`gfa.allowable` is the one canonical allowable-GFA transform; TWA GFAAgent should point here.

```bash
pip install -r rewire/requirements.txt   # sg-urban-spine@main + pinned upstream deps (python 3.11)
cd rewire
spine --path . validate */transform.yaml
spine --path . test                      # golden: both transforms
python -m pytest -q tests                # Bronze <-> Silver round trip
```

## How it works

```
upstream KG (Zenodo, N-Quads)
   │  one-off: Blazegraph + rewire/bronze/extract_kg.py
   ▼
Bronze  ura_mp2019_kg/<version>/   one Parquet per upstream retriever (exact return value) + _schema.json
Bronze  lta_road_network/<version>/ road_network.shp|parquet
   │  ura.mp2019_conform  (kg_codec.bronze_to_silver)
   ▼
Silver  plots · zoning · regulations · roads · id_bridge      (EPSG:3414)
   │  gfa.allowable: kg_codec.silver_to_frames -> retrievers_from_frames
   │                 -> upstream_harness.run_estimate_allowable_gfas -> UNMODIFIED main.estimate_allowable_gfas
   ▼
Gold    allowable_gfa · allowable_gfa_by_programme · allowable_gfa_uncomputed
```

Gold layout: upstream returns a `{programme: gfa}` dict per plot. `allowable_gfa_by_programme` keeps
every computed value; `allowable_gfa` is one row per plot (max over programmes, which programme, how
many); `allowable_gfa_uncomputed` lists the (plot, programme) pairs where upstream emits NaN (spine
Gold columns are non-null, so NaN is a row here rather than a null). Upstream's `None` programme key
is written as `unspecified`.

End to end through a spine lake (Bronze → conformer → transform → WAP audit → publish), on the
1188-plot fixture, all plots: 862 plots with GFA, 1964 programme values, 27 uncomputed, ~15 s:

```bash
C=ura.mp2019_conform/golden/sg_44_plots/inputs
spine --lake LAKE bronze-add --url https://doi.org/10.5281/zenodo.14646814 --license CC-BY-4.0 ura_mp2019_kg sg44-v1 $C/kg/*
spine --lake LAKE bronze-add --url https://doi.org/10.5281/zenodo.14647555 --license "CC-BY-4.0 (Zenodo redistribution of LTA data, Singapore Open Data Licence v1.0)" lta_road_network sg44-v1 $C/roads/*
spine --lake LAKE --path . run ura.mp2019_conform
spine --lake LAKE --path . run gfa.allowable
```

* **upstream_harness.py** imports upstream `main.py` and, for one call, rebinds only I/O names in
  its namespace: the 12 SPARQL retrievers (served from Silver) and `instantiate_allowed_gfa`
  (captured instead of writing N-Quads). Every `utils.py` step (GPR assignment, street block plans,
  road buffers, party walls, UDG setbacks, footprints, storeys, GFA) runs as published. An optional
  scope narrows work to target plots at the `set_plot_edge_setbacks` hook, *after* all plot context
  (neighbours, roads, regulations) is computed for every plot in Silver.
* **kg_codec.py** is the single mapping between upstream frames and Silver, in both directions, so
  conformer and transform cannot drift. Tests check the round trip is exact for all 12 retrievers.
* **No Blazegraph at runtime.** Blazegraph (upstream's jar, ran on Linux with OpenJDK 17) is only
  needed once, to snapshot the published KG into Bronze (`rewire/bronze/extract_kg.py`).

## IDs (id_bridge, GERS pattern)

* Hub `plot_id = "mp19-" + lower(INC_CRC)` (URA MP2019 feature checksum), same convention as the
  spine's Jurong example. The KG splits a few MP2019 features into several CityObjects (slivers):
  the largest keeps `mp19-<inc_crc>`, the others get `-p2`, `-p3`… by area then IRI.
* `id_bridge` rows: `twa_citieskg_plot` (KG CityObject IRI → plot, every plot) and
  `ura_mp2019_inc_crc` (INC_CRC → primary plot), `lta_road_network` (`UNIQUE_ID` → `lta-rd-<UNIQUE_ID>`).
* Upstream neighbour sets are stored as hub ids in `plots.neighbour` and mapped back to IRIs by the transform.

## Silver mapping and the schema gap

| upstream | Silver |
|---|---|
| `get_plots` (MP2019 plot, LU_DESC, GPR) | `plots` (geometry → EPSG:3414, `area_m2`), `zoning.zone_type`, `zoning.gross_plot_ratio`, `zoning.lu_desc` |
| plot enrichments (`get_plot_properties`, `get_neighbours`, `get_plot_neighbour_types`, `get_plot_allowed_programmes`) | extra columns on `plots` (sets/lists as JSON text) |
| regulation catalogues (DC, SBP, UDG, LHA, HCP, road categories) + `get_regulation_links` | `regulations.applicable_regulations_json` |

Upstream links plots to regulations **many-to-many**. Silver has no plot↔regulation link table yet,
so each plot's row in `regulations` carries its links and, the first time a regulation appears, that
regulation's full content (attributes + WKB geometry) in upstream vocabulary. This is lossless but
means a reader must take all `regulations` rows of a `source_version`; `silver_to_frames` raises if a
referenced regulation has no content. A proper fix is a Silver `planning_regulations` +
`plot_regulation_links` pair (Data Vault link); drafted but **not** proposed, since it changes the
shared contract.

## Golden

Fixture `sg_44_plots` (both transforms): 44 target plots in 4 Singapore clusters — Tanjong Pagar
(12, commercial/hotel), Serangoon Gardens (12, landed housing), Jurong East (12, white/commercial),
and 8 residential plots under a Street Block Plan — plus every plot within 150 m (1188 plots total).

* Reference for `gfa.allowable`: the unmodified upstream chain on these inputs. Cross-checks:
  * identical over `PYTHONHASHSEED` 0/1/7/123, and whether fed from Bronze directly or via Silver;
  * identical (99/99 values) to the unmodified upstream run against the **full** KG in Blazegraph
    for the 36 plots that run covered;
  * 106/115 values equal to upstream's published output (Zenodo doi:10.5281/zenodo.14646814,
    `output_gfa_triples.nq`); the 9 that differ are all `Flat`. Two upstream runs against Blazegraph
    also differed from each other on 8 Flat/Condominium values, so upstream itself is not
    run-to-run stable when reading from SPARQL. Our guess is SPARQL result row order (not proven).
    Reading a frozen Bronze removes this variance.
* Reference for `ura.mp2019_conform`: self-consistency; Silver must rebuild the exact upstream frames
  (`tests/test_roundtrip.py`).
* Context rule: results for a plot depend on neighbours and roads; keep ≥150 m of context plots in
  Silver around the plots you compute.
* Upstream needs at least one Street Block Plan plot in the data (`get_min_rect_edges` fails on an
  empty frame), which is why the fixture has an SBP cluster.

## Upstream changes (outside rewire/)

1. `config.ini`: Windows `\` path separators → `/` so paths resolve on Linux (Windows still accepts `/`).

No other upstream file is modified.

## Licences

Code: MIT (`LICENSE`, ETH Zürich MIE Lab). Data: URA Master Plan 2019 and LTA road network under the
Singapore Open Data Licence v1.0; the planning KG and intermediate inputs on Zenodo under CC-BY-4.0
(doi:10.5281/zenodo.14646814, doi:10.5281/zenodo.14647555, ontologies doi:10.5281/zenodo.14585554).
Commercial use allowed with attribution.

## Rebuilding Bronze from the published KG

```bash
# 1. data: Zenodo 14647555 -> data_submission/input, 14646814 -> data_submission/output
# 2. Blazegraph (upstream jar; tested with OpenJDK 17, -Xmx20g on a 32 GB box). Load the Zenodo
#    N-Quads into the namespaces in config.ini with upstream blazegraph_setup.py
#    (create_namespace / upload_nquads). The journal for the full KG is ~11 GB.
java -server -Xmx20g -jar blazegraph.jar &
# 3. snapshot (scope optional; omit for island-wide). Needs ~6 GB RAM per process; run one at a time
#    next to Blazegraph (running three in parallel got Blazegraph OOM-killed here).
python rewire/bronze/extract_kg.py OUT_DIR --scope plot_iris.txt --context-m 150
spine bronze-add --url https://doi.org/10.5281/zenodo.14646814 --license CC-BY-4.0 ura_mp2019_kg <version> OUT_DIR/*
```

## Readiness

* `ura.mp2019_conform`: RL5. Exact round trip on a 1188-plot Singapore extract. Not yet run island-wide.
* `gfa.allowable`: RL6. Golden passes against the unmodified upstream; not yet run island-wide
  through the spine.
