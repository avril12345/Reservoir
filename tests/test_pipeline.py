"""Pipeline chunk: the synthetic region (tests/synthetic.py) run offline through analyse -> score -> outputs,
checked against hand-worked answers, a golden snapshot, and a second identical run.

    pytest -m pipeline
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest

import synthetic as S
from reservoir.config import load_region
from reservoir.fetch import ACRE_M2

pytestmark = pytest.mark.pipeline
GOLDEN = Path(__file__).parent / "golden"
STATES = {"MA": "ma_ngrid_blackstone", "NY": "ny_hudson_valley"}

# Known gaps found by this suite. Each is a strict xfail: it turns into a failure once fixed, as a reminder to
# delete the entry.
KNOWN_GAPS = {
    ("NY", "CEMETERY"): "NY has no text use description (use_desc_field = PROP_CLASS), so the CEMETERY/SCHOOL/AIRPORT "
                        "keyword exclusion never fires, and site_type_from_use maps ORPS 695 (cemetery) to 'other'.",
}


@pytest.fixture(scope="session")
def synth(tmp_path_factory):
    cache = {}

    def get(state: str, **kw):
        k = (state, tuple(sorted(kw.items())))
        if k not in cache:
            cache[k] = S.run_pipeline(load_region(STATES[state]), tmp_path_factory.mktemp(f"synth_{state}"), **kw)
        return cache[k]
    return get


def _site(res, pid):
    return S.by_parcel(res).loc[pid]


# --------------------------------------------------------------------------------- hand-worked answers
CASES = [(st, pid, field, val) for st in STATES for pid, exp in S.EXPECT.items() for field, val in exp.items()]


@pytest.mark.parametrize("state,pid,field,expected", [
    pytest.param(*c, marks=pytest.mark.xfail(strict=True, reason=KNOWN_GAPS[(c[0], c[1])]) if (c[0], c[1]) in KNOWN_GAPS else ())
    for c in CASES], ids=[f"{c[0]}-{c[1]}-{c[2]}" for c in CASES])
def test_parcel_matches_hand_worked_answer(synth, report, state, pid, field, expected):
    s = _site(synth(state), pid)
    got = {"passes": s.passes_filters}.get(field, s.get(field))
    report.row("Pipeline: synthetic parcels vs. hand-worked answers",
               {"state": state, "parcel": pid, "field": field, "expected": expected, "got": got if not isinstance(got, float) else round(got, 3)})
    if isinstance(expected, float):
        assert got == pytest.approx(expected, abs=0.06), f"{pid}.{field}"
    else:
        assert got == expected, f"{pid}.{field}"


def test_score_formula_on_known_parcels(synth):
    """Weighted sum, worked by hand with config/weights/default.yaml."""
    b = S.by_parcel(synth("MA"))
    # IND: site .85, headroom 1, distance 1, size 1, permitting 1, incentives 0, land cover 1
    assert b.loc["IND", "score"] == pytest.approx(87.5, abs=0.05)
    # LANDFILL: site 1, incentives .5 SMART + .25 EJ -> 0.75
    assert b.loc["LANDFILL", "c_incentives"] == pytest.approx(0.75)
    assert b.loc["LANDFILL", "score"] == pytest.approx(98.0, abs=0.05)
    # MORATORIUM: 0 MW headroom, active solar moratorium caps permitting at 0.05
    assert b.loc["MORATORIUM", "c_permitting"] == pytest.approx(0.05)
    assert b.loc["MORATORIUM", "score"] == pytest.approx(58.0, abs=0.05)


def test_buildable_area_geometry(synth):
    b = S.by_parcel(synth("MA"))
    assert b.loc["IND", "buildable_acres"] == pytest.approx(370 * 370 / ACRE_M2, rel=1e-3)       # 15 m setback
    assert b.loc["FLOOD", "buildable_acres"] == pytest.approx(185 * 370 / ACRE_M2, rel=1e-3)     # west half of core in SFHA
    assert b.loc["BROWNFIELD", "building_share"] > 0                                             # building + 10 m removed
    assert b.loc["BROWNFIELD", "buildable_acres"] < 270 * 270 / ACRE_M2
    assert b.loc["STEEP", "steep_share"] > 0.9 and b.loc["STEEP", "largest_contig_acres"] < 0.5
    assert b.loc["WET", "wetland_share"] == pytest.approx(1.0)


def test_assemblage_joins_same_owner_lots_but_not_across_a_road(synth):
    res = synth("MA")
    sc = res["scored"]
    asm = sc[sc.member_ids.str.contains("ASM_A")].iloc[0]
    assert set(asm.member_ids.split("; ")) == {"ASM_A", "ASM_B"} and asm.n_parcels == 2
    assert asm.passes_filters, "two 3.9 ac lots fail alone (2.2 ac buildable each) but pass joined (5.2 ac)"
    assert asm.largest_contig_acres == pytest.approx(210 * 100 / ACRE_M2, rel=1e-3)
    road = sc[sc.member_ids.str.contains("ROAD_")]
    assert len(road) == 2 and (road.n_parcels == 1).all(), "a mapped road between same-owner lots makes two sites"


def test_without_roads_layer_lots_across_gap_join_and_are_flagged(synth):
    sc = synth("MA", with_roads=False, write_outputs=False)["scored"]
    road = sc[sc.member_ids.str.contains("ROAD_A")].iloc[0]
    assert road.n_parcels == 2 and road.gap_joined
    assert any("gap" in f.lower() for f in road.site_flags)


def test_small_lots_dropped_and_every_lot_accounted_for(synth):
    res = synth("MA")
    pid = res["region"].parcel_fields["parcel_id"]
    assert "TINY" not in set(res["lots"][pid]), "0.6 ac lot is below assemblage.min_lot_acres"
    members = [m for ids in res["sites"].member_ids for m in ids.split("; ")]
    assert sorted(members) == sorted(p.id for p in S.PARCELS if p.id != "TINY")
    assert len(members) == len(set(members)), "a lot belongs to exactly one site"


def test_flags_explain_the_score(synth):
    b = S.by_parcel(synth("MA"))
    flags = lambda pid: " | ".join(b.loc[pid, "site_flags"])
    assert "0 MW headroom" in flags("MORATORIUM") and "Solar moratorium" in flags("MORATORIUM")
    assert "Landfill:" in flags("LANDFILL") and "TESTTOWN LANDFILL" in flags("LANDFILL")
    assert "Brownfield: 2-0001234" in flags("BROWNFIELD")
    assert "forest" in flags("FOREST") and "SFHA" in flags("FLOOD")
    assert "not yet human-verified" not in flags("IND")


def test_ranks_are_contiguous_and_follow_score(synth):
    sc = synth("MA")["scored"]
    p = sc[sc.passes_filters]
    assert sorted(p["rank"].astype(int)) == list(range(1, len(p) + 1))
    assert p.sort_values("rank").score.is_monotonic_decreasing
    assert sc[~sc.passes_filters]["rank"].isna().all()


def test_reweighting_moves_the_ranking(synth):
    """The app re-weights live; dropping the permitting and headroom weights must lift the moratorium site."""
    from reservoir import scoring
    from reservoir.bylaws import tracker

    res = synth("MA", write_outputs=False)
    r = res["region"]
    base = S.by_parcel(res).loc["MORATORIUM", "rank"]
    r.weights["weights"] = {**r.weights["weights"], "permitting": 0, "grid_hosting": 0}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tracker, "load", lambda path=None: S.tracker_records(r.state))
        sc = scoring.score(r, res["sites"])
    pid = r.parcel_fields["parcel_id"]
    assert sc.set_index(pid).loc["MORATORIUM", "rank"] < base


# ------------------------------------------------------------------------------------------- outputs
def test_output_files_are_written_and_consistent(synth):
    res = synth("MA")
    d, sc = res["out_dir"], res["scored"]
    short = pd.read_csv(d / "shortlist.csv")
    assert len(short) == int(sc.passes_filters.sum())
    assert {"rank", "score", "parcel_id", "owner_name", "site_flags", "est_mwac"} <= set(short.columns)
    geo = gpd.read_file(d / "shortlist.geojson")
    assert geo.crs.to_epsg() == 4326 and geo.geometry.is_valid.all() and len(geo) == len(short)
    lots = pd.read_csv(d / "shortlist_lots.csv")
    assert set(lots.parcel_id) >= {"ASM_A", "ASM_B"}, "the lot list behind each site lists every tax lot"
    for layer in ("sites", "lots"):
        assert len(gpd.read_file(d / "scored_parcels.gpkg", layer=layer))


def test_owner_list_groups_by_owner_and_mailing_address(synth):
    o = synth("MA")["owners"]
    acme = o[o.owner_name == S.ACME].iloc[0]
    assert acme.sites == 2 and set(acme.towns.split(", ")) == {S.TOWN_A, S.TOWN_B}   # IND + MORATORIUM
    assert o.loc[o.owner_name == "TOWN OF TESTTOWN", "owner_type"].iloc[0] == "public"
    assert o.loc[o.owner_name == "SMITH FAMILY TRUST", "parcels"].iloc[0] == 2
    assert o.sites.sum() == len(synth("MA")["short"])


def test_site_cards_render(synth):
    res = synth("MA")
    assert len(res["cards"]) == 5
    html = Path(res["cards"][0]).read_text(encoding="utf-8")
    assert "data:image/png;base64," in html and str(res["short"].iloc[0]["parcel_id"]) in html
    assert (res["out_dir"] / "cards" / "all_cards.html").stat().st_size > 10_000


# ------------------------------------------------------------------------------------- repeatability
SNAP_COLS = ["member_ids", "site_type", "passes_filters", "rank", "score", "buildable_acres", "largest_contig_acres", "est_mwac",
             "c_site_type", "c_grid_hosting", "c_grid_distance", "c_size", "c_permitting", "c_incentives", "c_land_cover"]


def _snapshot(res) -> pd.DataFrame:
    s = res["scored"][SNAP_COLS].copy()
    s["member_ids"] = s.member_ids.map(lambda m: "; ".join(sorted(m.split("; "))))
    return s.sort_values("member_ids").reset_index(drop=True).round(3)


def test_second_run_is_identical(synth, tmp_path):
    a = _snapshot(synth("MA"))
    b = _snapshot(S.run_pipeline(load_region(STATES["MA"]), tmp_path, write_outputs=False))
    pd.testing.assert_frame_equal(a, b)


@pytest.mark.parametrize("state", list(STATES))
def test_matches_golden_snapshot(synth, report, update_golden, state):
    """Catches any change in scores or site types; rerun with --update-golden after an intended change."""
    snap = _snapshot(synth(state))
    path = GOLDEN / f"synthetic_{state}.csv"
    if update_golden or not path.exists():
        GOLDEN.mkdir(exist_ok=True)
        snap.to_csv(path, index=False)
        pytest.skip(f"golden snapshot written: {path.name}")
    gold = pd.read_csv(path, keep_default_na=False, na_values=[""])
    snap = pd.read_csv(pd.io.common.StringIO(snap.to_csv(index=False)), keep_default_na=False, na_values=[""])
    diff = []
    for _, (g, s) in enumerate(zip(gold.itertuples(index=False), snap.itertuples(index=False))):
        for c in SNAP_COLS:
            gv, sv = getattr(g, c), getattr(s, c)
            same = (pd.isna(gv) and pd.isna(sv)) or (gv == sv) or (isinstance(gv, float) and isinstance(sv, float) and abs(gv - sv) <= 0.001)
            if not same:
                diff.append({"state": state, "site": g.member_ids, "column": c, "golden": gv, "now": sv})
    if len(gold) != len(snap):
        diff.append({"state": state, "site": "*", "column": "row count", "golden": len(gold), "now": len(snap)})
    if diff:
        report.table(f"Pipeline: golden snapshot differences ({state})", diff,
                     "If the change is intended, rerun with --update-golden and commit tests/golden/.")
    assert not diff, f"{len(diff)} values differ from {path.name}: {diff[:5]}"


@pytest.mark.parametrize("pid", [p.id for p in S.PARCELS if p.id in S.EXPECT])
def test_states_score_the_same_world_the_same_way(synth, pid):
    """The core is state-agnostic: the same world under MA and NY config gives the same score."""
    if ("NY", pid) in KNOWN_GAPS:
        pytest.xfail(KNOWN_GAPS[("NY", pid)])
    ma, ny = _site(synth("MA"), pid), _site(synth("NY"), pid)
    assert (ma.site_type, ma.passes_filters) == (ny.site_type, ny.passes_filters)
    assert ma.score == pytest.approx(ny.score, abs=0.05)


def test_summary_written_to_report(synth, report):
    res = synth("MA")
    sc = res["scored"]
    report.note("Pipeline: synthetic run", f"{len(res['lots'])} lots -> {len(sc)} sites, {int(sc.passes_filters.sum())} pass filters; "
                f"layers: {json.dumps(res['counts'])}")


def test_tracker_setback_flows_into_buildable_geometry(tmp_path):
    """Same synthetic world, but town A's bylaw sets a 100 ft solar setback: IND shrinks by 30.48 m a side."""
    from reservoir.bylaws import tracker

    r = load_region(STATES["MA"])
    recs = S.tracker_records(r.state)
    k = tracker.key(r.state, S.TOWN_A)
    recs[k] = recs[k].model_copy(update={"solar_setback_ft": 100.0})
    b = S.by_parcel(S.run_pipeline(r, tmp_path, write_outputs=False, records=recs))
    side = 400 - 2 * 100 * 0.3048
    assert b.loc["IND", "setback_source"] == "tracker" and b.loc["IND", "setback_ft_applied"] == 100
    assert b.loc["IND", "buildable_acres"] == pytest.approx(side * side / ACRE_M2, rel=1e-3)
    assert "Property-line setback 100 ft from siting tracker" in b.loc["IND", "site_flags"]
