"""Extensibility chunk: how well the program takes new towns and new states.

    pytest -m extensibility
    pytest -m extensibility --region ny_hudson_valley

Three parts:
  * Config contract (hard failures for every region): YAML parses, CRS is metric, weights cover every site
    type and permission the code can emit.
  * Region readiness (one check per region x requirement): hard failures for regions marked
    `status: production`; for `configured` / `scaffold` regions a missing piece is reported as a known gap
    (xfail), and the report shows the full readiness matrix.
  * Framework behaviour (hard failures): adding towns through config or --towns, fetching N towns offline,
    town locality of scores, and running the whole pipeline under every state's config, including a
    brand-new state that exists only as YAML.
"""
from __future__ import annotations

import copy
import re
import typing
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from pyproj import CRS, Transformer
from shapely.geometry import Point, box

import synthetic as S
from reservoir import analysis
from reservoir.bylaws import schema
from reservoir.config import CONFIG, ROOT, list_regions, load_region

pytestmark = pytest.mark.extensibility
REGIONS = list_regions()
STATUSES = ("production", "configured", "scaffold")
# Site types analysis.analyse() can assign on top of what the use-code mapping returns.
ANALYSIS_TYPES = {"landfill", "brownfield", "gravel_pit", "disturbed_land", "conservation", "excluded_use", "residential", "other"}


def _emitted_use_types(state: str) -> set[str]:
    return {analysis.site_type_from_use(state, f"{c:03d}") for c in range(0, 1000)} | {analysis.site_type_from_use(state, f"{c}0") for c in range(100, 1000)}


# ------------------------------------------------------------------------------------- config contract
@pytest.mark.parametrize("region_id", REGIONS)
def test_region_yaml_contract(region_id):
    import yaml

    raw = yaml.safe_load((CONFIG / "regions" / f"{region_id}.yaml").read_text(encoding="utf-8"))
    assert raw["id"] == region_id, "id must match the file name (CLI and outputs/ use it)"
    for k in ("name", "state", "utility", "status"):
        assert raw.get(k), f"missing `{k}`"
    assert raw["status"] in STATUSES
    assert (CONFIG / "sources" / f"{raw['state'].lower()}.yaml").exists(), "no config/sources/<state>.yaml"
    assert (CONFIG / "weights" / f"{raw.get('weights', 'default')}.yaml").exists()
    r = load_region(region_id)
    assert r.towns == [t.upper() for t in r.towns]


@pytest.mark.parametrize("region_id", REGIONS)
def test_crs_is_projected_in_metres(region_id):
    crs = CRS.from_epsg(load_region(region_id).crs)
    assert crs.is_projected and crs.axis_info[0].unit_name in ("metre", "meter"), \
        "setbacks, buffers and acreage are computed in CRS units and assume metres"


@pytest.mark.parametrize("weights", sorted(p.stem for p in (CONFIG / "weights").glob("*.yaml")))
def test_weights_profile_covers_everything_the_code_emits(weights):
    import yaml

    w = yaml.safe_load((CONFIG / "weights" / f"{weights}.yaml").read_text(encoding="utf-8"))
    comps = {"site_type", "grid_hosting", "grid_distance", "size", "permitting", "incentives", "land_cover"}
    assert set(w["weights"]) == comps and all(v >= 0 for v in w["weights"].values()) and sum(w["weights"].values()) > 0
    states = {load_region(r).state for r in REGIONS}
    emitted = set().union(*(_emitted_use_types(s) for s in states)) | ANALYSIS_TYPES
    missing = emitted - set(w["site_type_scores"])
    assert not missing, f"site types without a score (they silently fall back to 'other'): {missing}"
    assert all(0 <= v <= 1 for v in w["site_type_scores"].values())
    perms = set(typing.get_args(schema.Permission)) | {"moratorium"}
    assert perms <= set(w["permitting_scores"]), f"missing permitting scores: {perms - set(w['permitting_scores'])}"
    pj = w["project"]
    assert pj["target_mwac"][0] < pj["target_mwac"][1] and pj["acres_per_mwac"] > 0
    assert pj["min_contiguous_acres_priority"] <= pj["min_contiguous_acres"]


# ------------------------------------------------------------------------------------------- readiness
def _readiness(r) -> dict[str, str]:
    """requirement -> '' if met, else what's missing."""
    s, p = r.sources, r.sources.get("parcels", {})
    tsrc = s.get("utility_territory") or s.get("towns") or {}
    url = lambda k: (s.get(k) or {}).get("url")
    hc = r.hosting() or {}
    out = {}
    out["towns listed"] = "" if (r.towns or (r.all_utility_towns and tsrc.get("utility_field"))) else \
        "no towns in the region YAML (fetch would send `IN ()`), and all_utility_towns is off or unsupported"
    out["town boundary layer"] = "" if tsrc.get("url") and (tsrc.get("town_field") or tsrc.get("name_field")) and tsrc.get("id_field") \
        else "towns / utility_territory url, name field or id field missing"
    out["parcel layer"] = "" if p.get("url") and p.get("id_field") and p.get("town_id_field") and p.get("use_code_field") \
        else "parcels url / id_field / town_id_field / use_code_field missing"
    of = p.get("owner_fields") or {}
    out["owner name + address"] = "" if of.get("name") and of.get("address") else "owner name/address not mapped: no assemblages, empty owner list"
    types = _emitted_use_types(r.state) - {"other"}
    out["use-code mapping"] = "" if len(types) >= 4 else \
        f"analysis.site_type_from_use has no {r.state} branch: every lot becomes 'other' (score 0.2), no residential exclusion"
    desc_is_text = p.get("use_desc_field") and p.get("use_desc_field") != p.get("use_code_field")
    out["use exclusions (cemetery/school/airport)"] = "" if desc_is_text else \
        (f"no text use description for {r.state} (" + ("use_desc_field is the use code" if p.get("use_desc_field") else "no use_desc_field")
         + "), so exclude_use_keywords only match owner names")
    out["tracker accepts state"] = "" if r.state in typing.get_args(schema.TownRecord.model_fields["state"].annotation) else \
        f"bylaws/schema.py: TownRecord.state Literal lacks {r.state}; permitting falls back to 'unknown'"
    out["hosting capacity (feeders)"] = "" if hc.get("feeders_url") or hc.get("feeders_file") else \
        f"no feeders_url/feeders_file for utility '{r.utility}': headroom unknown, distance 0 for every site"
    for k, label in [("protected_open_space", "protected open space"), ("brownfields_21e", "brownfield registry"),
                     ("landfills_poly", "landfill layer"), ("low_income_ej", "low-income / EJ layer"), ("buildings", "buildings")]:
        out[label] = "" if url(k) else f"`{k}.url` not set"
    return out


READINESS = [(rid, req) for rid in REGIONS for req in _readiness(load_region(rid))]


@pytest.mark.parametrize("region_id,requirement", READINESS, ids=[f"{a}-{b}" for a, b in READINESS])
def test_region_readiness(region_id, requirement, report):
    r = load_region(region_id)
    gap = _readiness(r)[requirement]
    status = r.raw.get("status", "scaffold")
    if gap and status != "production":
        pytest.xfail(f"{region_id}: {gap}")
    assert not gap, f"{region_id} is marked production but: {gap}"


def test_readiness_matrix_for_report(report):
    """Informational: requirement x region grid for the report (the pass/fail lives in test_region_readiness)."""
    regions = {rid: load_region(rid) for rid in REGIONS}
    checks = {rid: _readiness(r) for rid, r in regions.items()}
    reqs = list(next(iter(checks.values())))
    rows = [{"requirement": q, **{f"{rid} ({regions[rid].raw.get('status')})": "ready" if not checks[rid][q] else "GAP" for rid in REGIONS}}
            for q in reqs]
    rows.append({"requirement": "READY / TOTAL", **{f"{rid} ({regions[rid].raw.get('status')})": f"{sum(not v for v in checks[rid].values())}/{len(reqs)}"
                                                    for rid in REGIONS}})
    report.table("Extensibility: region readiness matrix", rows, "What each region still needs before `reservoir run` gives trustworthy "
                 "results. Details for every GAP are listed under Known gaps.")


# ------------------------------------------------------------------------------------ adding towns
def test_towns_from_yaml_and_cli_override_are_normalised(monkeypatch):
    from reservoir import cli, fetch

    seen = {}
    monkeypatch.setattr(fetch, "fetch_region", lambda r: seen.setdefault("towns", r.towns) and {})
    cli.main(["fetch", "--region", "ma_ngrid_blackstone", "--towns", "Mendon", "millville"])
    assert seen["towns"] == ["MENDON", "MILLVILLE"]


@pytest.mark.parametrize("all_towns", [False, True])
def test_resolve_towns_where_clause(monkeypatch, all_towns):
    from reservoir import arcgis, fetch

    r = load_region("ma_ngrid_blackstone")
    r.towns, r.all_utility_towns = ["UXBRIDGE", "MENDON"], all_towns
    calls = []

    def fake_query(url, where="1=1", **kw):
        calls.append(where)
        return gpd.GeoDataFrame({"TOWN": ["Uxbridge", "Mendon"], "TOWN_ID": [304, 179], "ELEC": ["National Grid"] * 2},
                                geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)], crs=r.crs)
    monkeypatch.setattr(arcgis, "query", fake_query)
    towns = fetch.resolve_towns(r)
    assert list(towns.town) == ["MENDON", "UXBRIDGE"]
    assert calls[0] == ("ELEC LIKE '%National Grid%'" if all_towns else "UPPER(TOWN) IN ('UXBRIDGE','MENDON')")


def test_resolve_towns_with_no_towns_fails_clearly(monkeypatch):
    """A region with an empty town list must stop with a clear message, not send `IN ()` to a server."""
    from reservoir import arcgis, fetch

    r = load_region("ma_ngrid_blackstone")
    r.towns, r.all_utility_towns = [], False
    sent = []
    monkeypatch.setattr(arcgis, "query", lambda url, where="1=1", **kw: sent.append(where) or gpd.GeoDataFrame(geometry=[], crs=r.crs))
    with pytest.raises(ValueError):
        fetch.resolve_towns(r)
    if sent and "IN ()" in sent[0]:
        pytest.xfail("resolve_towns sends `IN ()` before failing: validate region.towns first (it still raises, "
                     "but only because the server returned nothing)")


def _fake_sources(monkeypatch, r, town_boxes: dict[str, tuple]):
    """Offline stand-ins for every network call in fetch_region. Each town gets one parcel per layer
    hit, plus one feature shared by all towns (to check de-duplication across neighbouring bboxes)."""
    from reservoir import arcgis, fetch, grid, rasters

    crs = r.crs
    to_crs = Transformer.from_crs(4326, crs, always_xy=True)
    ids = {t: i + 1 for i, t in enumerate(town_boxes)}
    calls = []
    src = r.sources
    parcels_url, towns_url = src["parcels"]["url"], (src.get("utility_territory") or src["towns"])["url"]

    def query(url, where="1=1", bbox=None, out_fields="*", out_crs=4326, **kw):
        calls.append((url, where))
        if url == towns_url:
            return gpd.GeoDataFrame({"TOWN": list(town_boxes), "TOWN_ID": list(ids.values()), "ELEC": ["National Grid"] * len(ids)},
                                    geometry=[box(*b) for b in town_boxes.values()], crs=crs)
        if url == parcels_url:
            tid = int(re.search(r"= (\d+)", where).group(1))
            t = next(k for k, v in ids.items() if v == tid)
            x0, y0, x1, y1 = town_boxes[t]
            return gpd.GeoDataFrame({"LOC_ID": [f"{t}-1"], "TOWN_ID": [tid]}, geometry=[box(x0 + 10, y0 + 10, x0 + 300, y0 + 300)], crs=crs)
        cx, cy = to_crs.transform((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2) if bbox else (0, 0)
        return gpd.GeoDataFrame({"f": ["own", "shared"]}, geometry=[Point(cx, cy).buffer(5), box(0, 0, 10, 10)], crs=crs)

    monkeypatch.setattr(arcgis, "query", query)
    monkeypatch.setattr(grid, "fetch_feeders", lambda hc, bounds, crs: gpd.GeoDataFrame(
        {"feeder_id": ["F"], "hc_mw": [1.0]}, geometry=[Point(bounds[0], bounds[1])], crs=crs))
    monkeypatch.setattr(rasters, "fetch_dem", lambda *a, **k: "dem.tif")
    monkeypatch.setattr(rasters, "fetch_nlcd", lambda *a, **k: "nlcd.tif")
    return calls


@pytest.mark.parametrize("n_towns", [1, 3])
def test_fetch_scales_to_more_towns_offline(monkeypatch, tmp_path, report, n_towns):
    from reservoir import fetch

    r = S.isolate(load_region("ma_ngrid_blackstone"), tmp_path)
    names = ["UXBRIDGE", "MENDON", "MILLVILLE"][:n_towns]
    r.towns = names
    origin = S.ORIGINS[r.crs]
    boxes = {t: (origin[0] + 5000 * i, origin[1], origin[0] + 5000 * i + 4000, origin[1] + 4000) for i, t in enumerate(names)}
    calls = _fake_sources(monkeypatch, r, boxes)
    counts = fetch.fetch_region(r, progress=lambda m: None)
    import pyogrio

    layers = {n for n, _ in pyogrio.list_layers(r.out_dir / "inputs.gpkg")}
    parcels = gpd.read_file(r.out_dir / "inputs.gpkg", layer="parcels")
    wet = gpd.read_file(r.out_dir / "inputs.gpkg", layer="wetlands")
    report.row("Extensibility: offline fetch by number of towns",
               {"towns": n_towns, "requests": len(calls), "layers written": len(layers), "parcels": counts["parcels"], "wetlands": counts["wetlands"]})
    assert counts["towns"] == n_towns and counts["parcels"] == n_towns
    assert sorted(parcels._town.unique()) == sorted(names), "every feature is tagged with the town it was fetched for"
    # wetlands: NWI + state layer each return one town feature and one shared feature; the shared one appears once.
    assert counts["wetlands"] == n_towns + 1, "features returned for two neighbouring towns are de-duplicated"
    assert set(__import__("json").loads((r.out_dir / "rasters.json").read_text())) == set(names)
    assert {"towns", "parcels", "wetlands", "flood", "feeders"} <= layers


def test_adding_a_town_leaves_other_towns_scores_unchanged(tmp_path):
    """Per-site scores depend only on that site's neighbourhood, so screening more towns only adds rows."""
    from reservoir import scoring
    from reservoir.bylaws import tracker

    base = load_region("ma_ngrid_blackstone")
    both = S.run_pipeline(base, tmp_path / "both", write_outputs=False)
    r = S.isolate(base, tmp_path / "one")
    S.build_world(r)
    gpkg = r.out_dir / "inputs.gpkg"
    p = gpd.read_file(gpkg, layer="parcels")
    p[p._town == S.TOWN_A].to_file(gpkg, layer="parcels", driver="GPKG")
    sites, _ = analysis.analyse(r, progress=lambda m: None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tracker, "load", lambda path=None: S.tracker_records(r.state))
        one = scoring.score(r, sites)
    key = lambda df: df.set_index("member_ids")[["score", "passes_filters", "site_type", "buildable_acres"]].sort_index()
    a = key(both["scored"][both["scored"]._town == S.TOWN_A])
    pd.testing.assert_frame_equal(a, key(one))


# ------------------------------------------------------------------------------------ adding states
@pytest.mark.parametrize("region_id", REGIONS)
def test_pipeline_runs_under_every_region_config(region_id, tmp_path, report):
    """The synthetic world through each state's config: field names, CRS and hosting config all differ."""
    r = load_region(region_id)
    if r.crs not in S.ORIGINS:
        pytest.skip(f"add an origin for EPSG:{r.crs} to tests/synthetic.py ORIGINS")
    try:
        res = S.run_pipeline(r, tmp_path, cards=2)
    except Exception as exc:
        if r.raw.get("status") != "production":
            pytest.xfail(f"{region_id}: pipeline failed on synthetic data: {type(exc).__name__}: {exc}")
        raise
    sc, short = res["scored"], res["short"]
    types = sorted(set(sc.site_type))
    report.row("Extensibility: synthetic world under each region's config",
               {"region": region_id, "state": r.state, "crs": r.crs, "sites": len(sc), "passing": int(sc.passes_filters.sum()),
                "site types found": ", ".join(types), "IND score": round(float(S.by_parcel(res).loc["IND", "score"]), 1)})
    assert len(short) and {"parcel_id", "owner_name", "site_address", "score", "rank"} <= set(short.columns)
    src_cols = {v for v in r.parcel_fields.values() if v}
    assert (short.parcel_id != "").all(), "parcel_id must be populated from the state's id_field"
    assert not (src_cols - {"PROP_CLASS"}) & set(pd.read_csv(res["out_dir"] / "shortlist.csv", nrows=0).columns), \
        "source-specific columns leaked into the standard shortlist"


def test_brand_new_state_from_yaml_only(tmp_path, report):
    """What happens when someone adds config for a state the code has never seen (here: 'CT', copied from MA)."""
    from reservoir import scoring
    from reservoir.bylaws import tracker
    from reservoir.config import Region

    ma = load_region("ma_ngrid_blackstone")
    ct = Region(id="ct_test", name="CT test", state="CT", utility="National Grid", towns=["TESTTOWN"], all_utility_towns=False,
                sources=copy.deepcopy(ma.sources), national=ma.national, weights=copy.deepcopy(ma.weights), raw={"status": "scaffold"})
    r = S.isolate(ct, tmp_path)
    S.build_world(r)
    sites, _ = analysis.analyse(r, progress=lambda m: None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tracker, "load", lambda path=None: {})
        sc = scoring.score(r, sites)
    b = sc.set_index(r.parcel_fields["parcel_id"])
    with pytest.raises(Exception):
        schema.TownRecord(state="CT", municipality="X")
    gaps = [
        {"touchpoint": "reservoir/analysis.py site_type_from_use", "effect": f"all lots typed by use code become 'other' (RESID typed as {b.loc['RESID', 'site_type']!r}, passes={bool(b.loc['RESID', 'passes_filters'])})"},
        {"touchpoint": "reservoir/bylaws/schema.py TownRecord.state / ExtractionResult.state", "effect": "tracker records and bylaw extraction reject the state"},
    ]
    report.table("Extensibility: adding a brand-new state with YAML only", gaps,
                 "The pipeline ran end to end for 'CT' (config copied from MA). Registry layers still type landfills and brownfields, "
                 "but these code changes are needed before a new state's results can be trusted:")
    assert b.loc["LANDFILL", "site_type"] == "landfill" and b.loc["BROWNFIELD", "site_type"] == "brownfield"
    assert len(sc) == len(S.by_parcel({"region": r, "scored": sc}))


STATE_SPECIFIC = [
    (r'state == "MA"|state == "NY"|\("MA", "NY", "OR"\)|Literal\["MA", "NY", "OR"\]', "state branch / state list"),
    (r"MassDEP|ISO-NE|Article 97|SMART|21E|MassGIS|Chapter 61", "MA-specific wording in user-facing text"),
    (r"Shape__Area", "assumes a Web-Mercator Shape__Area field on the parcel layer"),
]


def test_report_state_specific_code(report):
    """Informational: where state-specific assumptions live in the code. Each is a place to touch for a new state."""
    rows = []
    for f in sorted((ROOT / "reservoir").rglob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for pat, kind in STATE_SPECIFIC:
                if re.search(pat, line) and not line.lstrip().startswith("#"):
                    rows.append({"file": f"{f.relative_to(ROOT).as_posix()}:{n}", "kind": kind, "code": line.strip()[:110]})
                    break
    report.table("Extensibility: state-specific code touchpoints", rows,
                 "Lines in reservoir/ that hard-code a state or MA-specific wording. Data-driven config would remove most of these.")
    assert rows is not None
