"""Unit chunk (with test_core.py and test_extract.py): scoring math and properties, grid joins, rasters,
owner typing and the tracker store. No files outside tmp_path, no network.

    pytest -m unit
"""
from __future__ import annotations

from datetime import date

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from reservoir import grid, outputs, rasters, scoring
from reservoir.analysis import owner_key
from reservoir.bylaws import accuracy, seed, tracker
from reservoir.bylaws.schema import TownRecord
from reservoir.config import load_region

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def region():
    return load_region("ma_ngrid_blackstone")


@pytest.fixture
def no_tracker(monkeypatch):
    monkeypatch.setattr(tracker, "load", lambda path=None: {})


def row(**kw):
    base = dict(largest_contig_acres=20.0, site_type="industrial_vacant", hc_mw=10.0, dist_3ph_m=50.0, dist_sub_m=5000.0,
                _town="NOWHERE", forest_share_buildable=0.0, wetland_share=0.0, flood_share=0.0, open_space_share=0.0,
                n_parcels=1, landfill_label="", brownfield_rtns="", conservation_use=False, energy_community=False,
                low_income=False, feeder_id="F1")
    return {**base, **kw}


def score_rows(region, rows):
    return scoring.score(region, pd.DataFrame(rows)).sort_index()


# ----------------------------------------------------------------------------------- scoring
def test_lin_endpoints():
    assert scoring._lin(100, 150, 2000) == 1.0 and scoring._lin(2000, 150, 2000) == 0.0
    assert scoring._lin(1075, 150, 2000) == pytest.approx(0.5)
    assert np.isnan(scoring._lin(np.nan, 150, 2000))


def test_seeded_property_sweep_bounds(region, no_tracker):
    """2,000 seeded random sites: every score in [0,100], every component in [0,1], ranks only on passing sites."""
    rng = np.random.default_rng(20261107)
    types = list(region.weights["site_type_scores"])
    n = 2000
    rows = [row(largest_contig_acres=float(rng.uniform(0, 80)), site_type=str(rng.choice(types)),
                hc_mw=float(rng.choice([np.nan, 0, rng.uniform(0, 20)])), dist_3ph_m=float(rng.choice([np.nan, rng.uniform(0, 6000)])),
                dist_sub_m=float(rng.uniform(0, 8000)), forest_share_buildable=float(rng.uniform(0, 1)),
                open_space_share=float(rng.uniform(0, 1)), energy_community=bool(rng.integers(2)), low_income=bool(rng.integers(2)))
            for _ in range(n)]
    s = score_rows(region, rows)
    assert s.score.between(0, 100).all()
    for c in [c for c in s if c.startswith("c_")]:
        assert s[c].between(0, 1).all(), c
    assert s["rank"].notna().sum() == s.passes_filters.sum()


@pytest.mark.parametrize("field,values", [
    ("hc_mw", [0, 0.5, 1, 3, 10]),                    # more headroom never hurts
    ("dist_3ph_m", [3000, 1500, 600, 150, 0]),        # closer never hurts
    ("largest_contig_acres", [1, 5, 10, 25, 60]),     # bigger never hurts (capped at max MW)
    ("forest_share_buildable", [1, 0.6, 0.3, 0]),     # less forest never hurts
])
def test_score_is_monotonic(region, no_tracker, field, values):
    s = score_rows(region, [row(**{field: v}) for v in values]).score.tolist()
    assert s == sorted(s), f"{field}: {list(zip(values, s))}"


def test_missing_grid_data_is_neutral_and_flagged(region, no_tracker):
    s = score_rows(region, [row(hc_mw=np.nan, dist_3ph_m=np.nan)]).iloc[0]
    assert s.c_grid_hosting == 0.5 and s.c_grid_distance == 0.0
    assert any("Hosting capacity unknown" in f for f in s.site_flags) and any("No 3-phase" in f for f in s.site_flags)


def test_substation_bonus_is_capped(region, no_tracker):
    s = score_rows(region, [row(dist_sub_m=100.0), row(dist_3ph_m=1000.0, dist_sub_m=100.0), row(dist_3ph_m=1000.0)])
    assert s.c_grid_distance.iloc[0] == 1.0
    assert s.c_grid_distance.iloc[1] == pytest.approx(s.c_grid_distance.iloc[2] + 0.1, abs=1e-3)


def test_priority_sites_use_lower_size_threshold(region, no_tracker):
    s = score_rows(region, [row(largest_contig_acres=3.0, site_type="landfill"), row(largest_contig_acres=3.0)])
    assert s.passes_filters.tolist() == [True, False]


@pytest.mark.parametrize("site_type", ["residential", "conservation", "excluded_use"])
def test_excluded_types_never_pass(region, no_tracker, site_type):
    assert not score_rows(region, [row(site_type=site_type, largest_contig_acres=80)]).passes_filters.iloc[0]


def test_article_97_flag_on_conservation_landfill(region, no_tracker):
    s = score_rows(region, [row(site_type="landfill", conservation_use=True)]).iloc[0]
    assert any("Article 97" in f for f in s.site_flags)


def test_unknown_site_type_falls_back_to_other(region, no_tracker):
    s = score_rows(region, [row(site_type="something_new")]).iloc[0]
    assert s.c_site_type == region.weights["site_type_scores"]["other"]


# --------------------------------------------------------------------------------------- grid
def test_nearest_feeder_and_max_distance():
    parcels = gpd.GeoDataFrame(geometry=[box(0, 0, 10, 10), box(10_000, 0, 10_010, 10)], crs=26986)
    feeders = gpd.GeoDataFrame({"feeder_id": ["A", "B"], "hc_mw": [1.0, 2.0]}, geometry=[Point(15, 5), Point(40, 5)], crs=26986)
    j = grid.nearest_feeder(parcels, feeders)
    assert j.loc[0, "feeder_id"] == "A" and j.loc[0, "dist_3ph_m"] == pytest.approx(5)
    assert pd.isna(j.loc[1, "dist_3ph_m"]), "beyond 5 km there is no feeder match"
    empty = grid.nearest_feeder(parcels, gpd.GeoDataFrame(geometry=[], crs=26986))
    assert len(empty) == 2 and empty.dist_3ph_m.isna().all()


def test_normalise_maps_portal_fields_and_epoch_dates():
    hc = {"feeder_id": "Master_CDF", "hc_mw": "HC_MW", "updated": "Updated_Date"}
    g = gpd.GeoDataFrame({"Master_CDF": ["X"], "HC_MW": ["2.5"], "Updated_Date": [1767225600000]}, geometry=[Point(0, 0)], crs=26986)
    n = grid.normalise(g, hc)
    assert n.loc[0, "feeder_id"] == "X" and n.loc[0, "hc_mw"] == 2.5 and n.loc[0, "updated"] == "2026-01-01"


def test_mask_points_cover_pixels():
    m = np.zeros((4, 4), bool)
    m[0, 0] = True
    xs, ys = grid._mask_points(m, (0, 0, 20, 20), 5.0, stride=1)
    assert (xs[0], ys[0]) == (2.5, 17.5)


# ------------------------------------------------------------------------------------ rasters
def test_slope_of_a_30pct_plane(tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    z = np.tile(np.arange(50) * 10 * 0.30, (50, 1)).astype("float32")
    p = tmp_path / "plane.tif"
    with rasterio.open(p, "w", driver="GTiff", height=50, width=50, count=1, dtype="float32", crs="EPSG:26986",
                       transform=from_origin(0, 500, 10, 10)) as dst:
        dst.write(z, 1)
    s, _, _ = rasters.slope_pct(p)
    assert np.nanmedian(s) == pytest.approx(30.0, abs=0.01)
    assert len(rasters.steep_polygons(p, 15)) == 1 and len(rasters.steep_polygons(p, 35)) == 0


def test_mask_to_polygons_drops_specks():
    from rasterio.transform import from_origin

    m = np.zeros((10, 10), bool)
    m[0, 0] = True                     # 100 m2 speck
    m[5:9, 5:9] = True                 # 1,600 m2 block
    gs = rasters.mask_to_polygons(m, from_origin(0, 100, 10, 10), "EPSG:26986", min_area_m2=400)
    assert len(gs) == 1 and gs.iloc[0].area == pytest.approx(1600)


# ------------------------------------------------------------------------------- owners / keys
@pytest.mark.parametrize("name,expected", [
    ("TOWN OF UXBRIDGE", "public"), ("COMMONWEALTH OF MASS", "public"), ("NEW ENGLAND POWER CO", "utility"),
    ("SMITH JOHN TR", "trust"), ("ACME REALTY LLC", "business"), ("JANE DOE", "individual"), (None, "individual"),
])
def test_owner_type(name, expected):
    assert outputs.owner_type(name) == expected


@pytest.mark.parametrize("name,addr,expected", [
    ("Acme, Inc.", "1 Main St.", "ACME INC|1 MAIN ST"), ("ACME", "", ""), ("", "1 MAIN ST", ""),
    ("UNKNOWN", "1 MAIN ST", ""), ("nan", "1 MAIN ST", ""),
])
def test_owner_key(name, addr, expected):
    assert owner_key(name, addr) == expected


def test_absentee_owner_note(region):
    df = pd.DataFrame([{"owner_name": "A LLC", "owner_care_of": "", "owner_address": "1 X ST", "owner_city": "C", "owner_state": "CT",
                        "owner_zip": "1", "parcel_id": "1", "_town": "T", "site_address": "", "parcel_acres": 10.0, "buildable_acres": 5.0,
                        "est_mwac": 1.0, "score": 50.0, "rank": 1.0, "site_type": "industrial"}])
    assert "Absentee" in outputs.owner_list(region, df).outreach_note.iloc[0]


# ------------------------------------------------------------------------------------- tracker
@pytest.mark.parametrize("name", ["Uxbridge", "TOWN OF UXBRIDGE", "  uxbridge ", "Town of Uxbridge"])
def test_tracker_key_normalises(name):
    assert tracker.key("ma", name) == "MA|UXBRIDGE"


def test_tracker_roundtrip_and_seed_protects_verified(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    human = TownRecord(state="NY", municipality="Cortlandt", solar_permission="special_permit", verified_by="AW", verified_on=date(2026, 11, 1))
    tracker.save({tracker.key("NY", "Cortlandt"): human}, path)
    assert tracker.load(path)[tracker.key("NY", "Cortlandt")] == human
    monkeypatch.setattr(seed, "load", lambda: tracker.load(path))
    monkeypatch.setattr(seed, "save", lambda recs: tracker.save(recs, path))
    seed.write_seeds()
    after = tracker.load(path)
    assert after[tracker.key("NY", "Cortlandt")].verified_by == "AW", "seed data must never overwrite a human-verified record"
    assert tracker.key("NY", "Waterford") in after


def test_shipped_tracker_file_is_valid():
    recs = tracker.load()
    assert recs, "data/bylaws/tracker.jsonl is empty"
    for k, r in recs.items():
        assert k == tracker.key(r.state, r.municipality)
        for m in r.moratoria:
            assert not (m.adopted and m.expires and m.expires < m.adopted), f"{k}: expires before adopted"


@pytest.mark.parametrize("kind,pred,truth,ok", [
    ("number", "50", "54", True), ("number", "50", "56", False), ("bool", "yes", "True", True),
    ("category", "By_Right", "by_right", True), ("date", "2026-12-31", "2026-12-30", False),
])
def test_accuracy_comparisons(kind, pred, truth, ok):
    assert accuracy._eq(kind, pred, truth) is ok
