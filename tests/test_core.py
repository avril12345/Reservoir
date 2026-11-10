from datetime import date

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from reservoir.analysis import site_type_from_use
from reservoir.arcgis import _esri_to_shapely
from reservoir.bylaws import accuracy, tracker
from reservoir.bylaws.schema import Evidence, Moratorium, TownRecord


def test_esri_polygon_with_hole():
    outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]          # clockwise = exterior
    hole = [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]]              # counter-clockwise = hole
    g = _esri_to_shapely({"rings": [outer, hole]})
    assert g.geom_type == "Polygon" and len(g.interiors) == 1
    assert g.area == pytest.approx(100 - 36)


def test_esri_lines_and_points():
    assert _esri_to_shapely({"paths": [[[0, 0], [1, 1]]]}).geom_type == "LineString"
    assert _esri_to_shapely({"x": 1, "y": 2}).x == 1
    assert _esri_to_shapely(None) is None


@pytest.mark.parametrize("code,expected", [
    ("410", "gravel_pit"), ("4400", "industrial_vacant"), ("441", "industrial_vacant"), ("400", "industrial"),
    ("424", "utility"), ("340", "commercial"), ("1010", "residential"), ("720", "agricultural"), ("390", "vacant_other"),
    (None, "other"),
])
def test_ma_use_codes(code, expected):
    assert site_type_from_use("MA", code) == expected


def test_ny_property_class():
    assert site_type_from_use("NY", "852") == "landfill"
    assert site_type_from_use("NY", "720") == "gravel_pit"
    assert site_type_from_use("NY", "340") == "industrial_vacant"


SCORES = {"by_right": 1.0, "site_plan_review": 0.9, "special_permit": 0.6, "not_addressed": 0.5,
          "unknown": 0.5, "moratorium": 0.05, "prohibited": 0.0}


def _mor(tech, status, expires):
    return Moratorium(technology=tech, local_law="LL 1-2026", status=status, adopted=None, effective=None,
                      expires=expires, duration_days=None, scope="", evidence=Evidence(quote="q", section="s"))


def test_moratorium_caps_score_and_expiry_is_respected():
    on = date(2026, 11, 6)
    active = TownRecord(state="NY", municipality="X", solar_permission="by_right", moratoria=[_mor("solar", "active", date(2026, 12, 31))])
    s, flags, label = tracker.permitting_assessment(active, SCORES, on)
    assert label == "moratorium" and s <= 0.05
    lapsed = TownRecord(state="NY", municipality="X", solar_permission="by_right", moratoria=[_mor("solar", "active", date(2026, 1, 1))])
    s2, flags2, label2 = tracker.permitting_assessment(lapsed, SCORES, on)
    assert label2 == "by_right" and s2 > 0.5
    assert any("check for extension" in f for f in flags2)
    proposed = TownRecord(state="NY", municipality="X", solar_permission="by_right", moratoria=[_mor("bess", "proposed", None)])
    s3, flags3, _ = tracker.permitting_assessment(proposed, SCORES, on)
    assert s3 == pytest.approx(0.7 * 1.0 + 0.3 * 0.5)            # proposed never moves the score
    assert any("Proposed" in f for f in flags3)


def test_bess_moratorium_only_dents_storage_share():
    rec = TownRecord(state="NY", municipality="X", solar_permission="by_right", bess_permission="by_right",
                     moratoria=[_mor("bess", "extended", date(2026, 12, 31))])
    s, _, label = tracker.permitting_assessment(rec, SCORES, date(2026, 11, 6))
    assert label == "moratorium" and s == pytest.approx(0.7 + 0.3 * 0.05)


def test_wilson_interval():
    lo, hi = accuracy.wilson(18, 20)
    assert 0.68 < lo < 0.75 and 0.96 < hi < 0.99


def test_accuracy_report_and_taxonomy(tmp_path, monkeypatch):
    truth = tmp_path / "gt.csv"
    pd.DataFrame([
        {"state": "MA", "municipality": "A", "checked_by": "AW", "pred_solar_permission": "special_permit", "truth_solar_permission": "special_permit",
         "pred_solar_setback_ft": "50", "truth_solar_setback_ft": "52", "pred_moratorium_active": "False", "truth_moratorium_active": "false"},
        {"state": "MA", "municipality": "B", "checked_by": "AW", "pred_solar_permission": "unknown", "truth_solar_permission": "by_right",
         "pred_solar_setback_ft": "100", "truth_solar_setback_ft": "", "pred_moratorium_active": "True", "truth_moratorium_active": "false", "error_type": ""},
        {"state": "NY", "municipality": "C", "checked_by": "", "truth_solar_permission": "by_right"},  # not checked -> ignored
    ]).to_csv(truth, index=False)
    monkeypatch.setattr(accuracy, "TRUTH", truth)
    rep = accuracy.report()
    assert rep["hand_checked_towns"] == 2
    assert rep["per_field"]["solar_permission"] == {"correct": 1, "checked": 2, "accuracy": 0.5, "ci95": rep["per_field"]["solar_permission"]["ci95"]}
    assert rep["per_field"]["solar_setback_ft"]["correct"] == 1   # within ±5 ft; blank truth skipped
    assert rep["error_types"]["omission"] == 1                    # unknown vs by_right
    assert rep["error_types"]["wrong_value"] == 1                 # moratorium True vs false


def test_report_without_truth(tmp_path, monkeypatch):
    monkeypatch.setattr(accuracy, "TRUTH", tmp_path / "missing.csv")
    assert accuracy.report()["hand_checked_towns"] == 0


@pytest.mark.parametrize("region_id", ["ny_hudson_valley", "or_pge"])
def test_parcel_fields_come_from_state_config(region_id):
    from reservoir import outputs
    from reservoir.config import load_region

    r = load_region(region_id)
    pf = r.parcel_fields
    raw = {src: f"{std}-value" for std, src in pf.items() if src}
    raw[pf["owner_zip"]] = None   # missing values must come out blank, not "nan"
    df = pd.DataFrame([{**raw, "_town": "X", "parcel_acres": 10.0, "buildable_acres": 6.0, "est_mwac": 1.0,
                        "score": 80.0, "rank": 1.0, "site_type": "industrial"}])
    s = outputs.standard_fields(r, df)
    for std, src in pf.items():
        assert s.loc[0, std] == ((raw[src] or "") if src else "")   # missing fields (e.g. OR site address) are blank
    assert not {"OWNER1", "SITE_ADDR", "LOC_ID", "USE_DESC"} & set(s.columns)
    g = outputs.owner_list(r, s)
    assert g.loc[0, "owner_name"] == "owner_name-value" and g.loc[0, "parcels"] == 1


def _lots():
    from reservoir.fetch import ACRE_M2

    X, ADDR = "ACME SAND & GRAVEL, INC.", "1 MAIN ST"
    rows = [  # (id, box, owner, mailing address, use code, town)
        ("A", (0, 0, 100, 100), X, ADDR, "440", "UXBRIDGE"),
        ("B", (100, 0, 200, 100), "Acme Sand & Gravel Inc", "1 Main St.", "410", "UXBRIDGE"),  # touches A; same key after normalising
        ("C", (215, 0, 300, 100), X, ADDR, "440", "UXBRIDGE"),        # 15 m gap from B (paper road)
        ("D", (340, 0, 440, 100), X, ADDR, "440", "UXBRIDGE"),        # 40 m gap from C: separate site
        ("E", (0, 100, 100, 200), "SOMEONE ELSE", ADDR, "440", "UXBRIDGE"),
        ("F", (0, -100, 100, 0), X, ADDR, "1010", "UXBRIDGE"),        # residential: never joins
        ("G", (440, 0, 540, 100), X, ADDR, "440", "DOUGLAS"),         # touches D, other town
        ("H", (1000, 0, 1100, 100), X, "", "440", "UXBRIDGE"),        # no mailing address -> no key
        ("I", (1100, 0, 1200, 100), X, "", "440", "UXBRIDGE"),
        ("J", (2000, 0, 2100, 100), X, ADDR, "440", "UXBRIDGE"),
        ("K", (2100.3, 0, 2200, 100), X, ADDR, "440", "UXBRIDGE"),    # 0.3 m digitising sliver
    ]
    df = gpd.GeoDataFrame([{"LOC_ID": i, "OWNER1": o, "OWN_ADDR": a, "USE_CODE": u, "USE_DESC": "", "_town": t}
                           for i, _, o, a, u, t in rows], geometry=[box(*b) for _, b, *_ in rows], crs=26986)
    df["parcel_acres"] = df.area / ACRE_M2
    return df


def test_assemblages_follow_owner_gap_town_and_use_rules():
    from reservoir.analysis import owner_key, prepare_lots
    from reservoir.config import load_region

    assert owner_key("TOWN OF UXBRIDGE", "") == ""          # never group on name alone
    lots = prepare_lots(load_region("ma_ngrid_blackstone"), _lots()).set_index("LOC_ID")
    aid = lots.assemblage_id
    assert aid["A"] == aid["B"] == aid["C"]                   # touching + 15 m gap, same owner key
    assert aid["D"] != aid["C"] and aid["E"] != aid["A"]      # 40 m gap; different owner
    assert aid["F"] != aid["A"] and not lots.eligible["F"]     # residential lot stays single
    assert aid["G"] != aid["D"]                               # never across a town line
    assert aid["H"] != aid["I"]                               # no owner key
    assert aid["J"] == aid["K"]
    assert lots.gap_joined["A"] and not lots.gap_joined["J"]  # A-B-C needs the gap; J-K only a sliver


def test_dissolve_removes_interior_lot_lines_before_setback():
    from reservoir.analysis import build_sites, prepare_lots
    from reservoir.config import load_region

    r = load_region("ma_ngrid_blackstone")
    sites = build_sites(r, prepare_lots(r, _lots()))
    abc = sites[sites.n_parcels == 3].iloc[0]
    assert abc.member_ids.split("; ")[0] in ("A", "B", "C") and set(abc.member_ids.split("; ")) == {"A", "B", "C"}
    assert abc.use_type == "gravel_pit"                       # best member use wins
    assert abc.lots_summary.startswith("3 lots, same owner,")
    assert len(abc.geometry.geoms) == 2                       # A+B merged; C stays across the paper road
    ab = max(abc.geometry.geoms, key=lambda g: g.area)
    assert ab.buffer(-15).area == pytest.approx(170 * 70, rel=0.01)   # vs 2 x 70 x 70 if lot line were set back
    jk = sites[sites.member_ids.str.contains("J")].iloc[0]
    assert jk.geometry.geom_type == "Polygon"                 # 0.3 m sliver closed


def test_public_road_splits_but_paper_road_and_frontage_road_do_not():
    from shapely.geometry import LineString

    from reservoir.analysis import assemble, owner_key, prepare_lots
    from reservoir.config import load_region

    assert owner_key("OWNER UNKNOWN", "1 MAIN ST") == ""
    lots = prepare_lots(load_region("ma_ngrid_blackstone"), _lots()).set_index("LOC_ID")
    frontage = LineString([(-50, -7), (600, -7)])            # runs past the fronts of A, B, C (not between them)
    in_gap = LineString([(207.5, -50), (207.5, 150)])        # built road in the 15 m B–C gap
    for roads, joined in (([frontage], True), ([frontage, in_gap], False)):
        a = assemble(lots.reset_index(), 20, gpd.GeoDataFrame(geometry=roads, crs=26986))
        a.index = lots.index
        assert bool(a.assemblage_id["B"] == a.assemblage_id["C"]) is joined
        assert a.assemblage_id["A"] == a.assemblage_id["B"]


# ---------------------------------------------------------------- town-specific property-line setbacks
def _setback_region():
    from reservoir.config import load_region

    return load_region("ma_ngrid_blackstone")      # default setbacks_m.property_line = 15 m


def _site(town, size=300.0):
    return gpd.GeoDataFrame({"_town": [town]}, geometry=[box(0, 0, size, size)], crs=26986)


def _patch_tracker(monkeypatch, *recs):
    monkeypatch.setattr(tracker, "load", lambda path=None: {tracker.key(r.state, r.municipality): r for r in recs})


def _core_acres(region, sites):
    from reservoir.analysis import property_setbacks
    from reservoir.fetch import ACRE_M2

    sb = property_setbacks(region, sites)
    return sites.geometry.buffer(-sb.setback_m).area / ACRE_M2, sb


def test_tracker_setback_shrinks_core_more_than_default(monkeypatch):
    r = _setback_region()
    _patch_tracker(monkeypatch, TownRecord(state="MA", municipality="Uxbridge", solar_setback_ft=100))
    acres, sb = _core_acres(r, pd.concat([_site("UXBRIDGE"), _site("DOUGLAS")], ignore_index=True))
    assert sb.setback_source.tolist() == ["tracker", "default"]          # two towns, two buffers
    assert sb.setback_ft_applied[0] == 100 and sb.setback_m[0] == pytest.approx(30.48)
    assert sb.setback_m[1] == 15 and sb.setback_ft_applied[1] == pytest.approx(15 / 0.3048)
    assert acres[0] == pytest.approx((300 - 2 * 30.48) ** 2 / 4046.8564224)
    assert acres[1] == pytest.approx(270 ** 2 / 4046.8564224)
    assert acres[0] < acres[1]


def test_tracker_record_without_setback_uses_default(monkeypatch):
    r = _setback_region()
    _patch_tracker(monkeypatch, TownRecord(state="MA", municipality="Uxbridge", solar_permission="by_right"))
    acres, sb = _core_acres(r, _site("UXBRIDGE"))
    assert sb.setback_source[0] == "default" and sb.setback_m[0] == 15     # by_right does not invent a setback
    assert acres[0] == pytest.approx(270 ** 2 / 4046.8564224)


def test_town_absent_from_tracker_uses_default(monkeypatch):
    _patch_tracker(monkeypatch)
    _, sb = _core_acres(_setback_region(), _site("SUTTON"))
    assert sb.setback_source[0] == "default" and sb.setback_m[0] == 15


def test_setback_flags_explain_source_and_unapplied_setbacks():
    from reservoir.analysis import setback_flags

    r = _setback_region()
    rec = TownRecord(state="MA", municipality="Uxbridge", solar_setback_ft=100, bess_setback_ft=150, from_residence_ft=200)
    recs = {tracker.key("MA", "Uxbridge"): rec}
    f = setback_flags(r, "UXBRIDGE", 100.0, "tracker", recs)
    assert f[0] == "Property-line setback 100 ft from siting tracker"
    assert any("200 ft from residences; not applied" in x for x in f)
    assert any("BESS setback 150 ft exceeds" in x for x in f)
    d = setback_flags(r, "SUTTON", 15 / 0.3048, "default", recs)
    assert d == ["Property-line setback 15 m (~49 ft default); town bylaw not in tracker"]
    # Tracker edited after geometry was computed: geometry used the default, tracker now says 100 ft.
    assert any("--reanalyse" in x for x in setback_flags(r, "UXBRIDGE", 15 / 0.3048, "default", recs))


def test_demo_redaction_masks_private_owners_consistently():
    from reservoir import outputs
    from reservoir.config import load_region

    r = load_region("ma_ngrid_blackstone")
    sites = pd.DataFrame({"OWNER1": ["SMITH JOHN", "DOE FAMILY TRUST", "ACME SAND LLC", "TOWN OF UXBRIDGE", "JONES MARY"],
                          "OWN_ADDR": ["1 ELM ST", "2 OAK ST", "3 MAIN ST", "21 S MAIN ST", "4 PINE ST"],
                          "OWN_CITY": ["UXBRIDGE"] * 5, "OWN_ZIP": ["01569"] * 5})
    lots = sites.iloc[[0, 2]].copy()
    s, l = outputs.redact_private_owners(r, sites, lots)
    assert s.OWNER1.tolist()[2:4] == ["ACME SAND LLC", "TOWN OF UXBRIDGE"]           # business / public kept
    masked = [s.OWNER1[0], s.OWNER1[1], s.OWNER1[4]]
    assert all(m.startswith("Private owner ") for m in masked) and len(set(masked)) == 3
    assert l.OWNER1.iloc[0] == s.OWNER1[0] and l.OWNER1.iloc[1] == "ACME SAND LLC"   # same label across frames
    assert s.OWN_CITY[0] == "" and "1 ELM ST" not in s.OWN_ADDR.tolist()
    assert sites.OWNER1[0] == "SMITH JOHN"                                           # inputs untouched
