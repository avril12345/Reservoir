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
