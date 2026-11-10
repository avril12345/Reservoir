"""Outputs chunk: checks on the real outputs/<region>/ written by `reservoir run` / `reservoir score`.

    pytest -m outputs                                 # every region that has been run
    pytest -m outputs --region ma_ngrid_blackstone    # one region

Three kinds of check:
  * internal consistency: ranges, formulas, filters, ranks, counts across files
  * independent recomputation: feeder distances and site-type evidence recomputed from inputs.gpkg
  * staleness: rescoring the cached metrics with today's code, weights and tracker reproduces the files
Regions that have not been run are skipped.
"""
from __future__ import annotations

import json
import pickle

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest

from reservoir.config import OUTPUTS, list_regions, load_region

pytestmark = pytest.mark.outputs
REGIONS = list_regions()
COMPONENTS = ["site_type", "grid_hosting", "grid_distance", "size", "permitting", "incentives", "land_cover"]
EXCLUDED_TYPES = {"residential", "conservation", "excluded_use"}


class Out:
    """Lazy loader for one region's output files."""

    def __init__(self, region_id):
        self.r = load_region(region_id)
        self.d = OUTPUTS / region_id
        self.meta = json.loads((self.d / "run_meta.json").read_text())
        self._c = {}

    def _get(self, k, fn):
        if k not in self._c:
            self._c[k] = fn()
        return self._c[k]

    short = property(lambda s: s._get("short", lambda: pd.read_csv(s.d / "shortlist.csv", dtype={"parcel_id": str, "owner_zip": str})))
    geo = property(lambda s: s._get("geo", lambda: gpd.read_file(s.d / "shortlist.geojson")))
    sites = property(lambda s: s._get("sites", lambda: gpd.read_file(s.d / "scored_parcels.gpkg", layer="sites")))
    lots = property(lambda s: s._get("lots", lambda: gpd.read_file(s.d / "scored_parcels.gpkg", layer="lots", ignore_geometry=True)))
    owners = property(lambda s: s._get("owners", lambda: pd.read_csv(s.d / "owner_list.csv")))
    cards = property(lambda s: sorted((s.d / "cards").glob("card_*.html")))

    def layer(self, name):
        import pyogrio

        path = self.d / "inputs.gpkg"
        names = {n for n, _ in pyogrio.list_layers(path)}
        return self._get(f"L_{name}", lambda: gpd.read_file(path, layer=name) if name in names else gpd.GeoDataFrame(geometry=[], crs=self.r.crs))


_CACHE: dict[str, Out] = {}


def pytest_generate_tests(metafunc):
    # Parametrize by `region_id` (not inside the fixture) so `--region` can select a single region.
    if "out" in metafunc.fixturenames:
        metafunc.parametrize("region_id", REGIONS)


@pytest.fixture
def out(region_id):
    if not (OUTPUTS / region_id / "run_meta.json").exists():
        pytest.skip(f"{region_id} has no outputs yet (run `reservoir run --region {region_id}`)")
    if region_id not in _CACHE:
        _CACHE[region_id] = Out(region_id)
    return _CACHE[region_id]


def _fail_rows(df, mask, cols, n=5):
    bad = df[mask]
    return f"{int(mask.sum())} rows, e.g.\n{bad[[c for c in cols if c in bad]].head(n).to_string()}"


# -------------------------------------------------------------------------------- files and counts
def test_expected_files_present(out):
    need = ["shortlist.csv", "shortlist.geojson", "shortlist_buildable.geojson", "shortlist_lots.csv", "scored_parcels.gpkg",
            "owner_list.csv", "run_meta.json", "inputs.gpkg", "fetch_summary.json", "cards/all_cards.html"]
    missing = [f for f in need if not (out.d / f).exists()]
    assert not missing, f"missing: {missing}"


def test_run_meta_matches_files(out):
    m = out.meta
    assert m["shortlist"] == len(out.short) == len(out.geo)
    assert m["owners"] == len(out.owners)
    assert m["cards"] == len(out.cards)
    assert m["sites_analysed"] == len(out.sites)
    assert m["passing_filters"] == int(out.sites.passes_filters.sum())
    assert m["lots"] == len(out.lots)


def test_fetch_summary_layers_nonempty(out, report):
    counts = json.loads((out.d / "fetch_summary.json").read_text())
    report.table(f"Outputs: input layer counts ({out.r.id})", [{"layer": k, "features": v} for k, v in counts.items()])
    for layer in ("parcels", "wetlands", "feeders"):
        assert counts.get(layer, 0) > 0, f"{layer} layer is empty: the whole territory would score without it"
    assert counts["towns"] == len(out.r.towns) or out.r.all_utility_towns


# ------------------------------------------------------------------------------- score internals
def test_scores_and_components_in_range(out):
    s = out.sites
    assert s.score.between(0, 100).all()
    for c in COMPONENTS:
        col = s[f"c_{c}"]
        assert col.notna().all() and col.between(0, 1).all(), f"c_{c} out of [0,1]"


def test_score_is_weighted_sum_of_components_with_current_weights(out):
    ws = out.r.weights["weights"]
    s = out.sites
    recomputed = 100 * sum(ws[c] * s[f"c_{c}"] for c in COMPONENTS) / sum(ws.values())
    bad = (recomputed - s.score).abs() > 0.15
    assert not bad.any(), ("score != weighted sum with config/weights/default.yaml (weights changed since the run? "
                           "rerun `reservoir score`): " + _fail_rows(s.assign(recomputed=recomputed), bad, ["parcel_id", "score", "recomputed"]))


def test_estimated_mw_follows_sizing_rule(out):
    pj = out.r.weights["project"]
    s = out.sites
    exp = np.minimum(s.largest_contig_acres / pj["acres_per_mwac"], pj["max_mwac"])
    bad = (exp - s.est_mwac).abs() > 0.011
    assert not bad.any(), _fail_rows(s, bad, ["parcel_id", "largest_contig_acres", "est_mwac"])


def test_hard_filters_applied_exactly(out):
    pj = out.r.weights["project"]
    smart = set(out.r.sources.get("smart", {}).get("priority_site_types", []))
    s = out.sites
    min_ac = np.where(s.site_type.isin(smart), pj["min_contiguous_acres_priority"], pj["min_contiguous_acres"])
    exp = (s.largest_contig_acres >= min_ac) & (s.open_space_share < 0.5) & ~s.site_type.isin(EXCLUDED_TYPES)
    bad = exp != s.passes_filters.astype(bool)
    assert not bad.any(), _fail_rows(s, bad, ["parcel_id", "site_type", "largest_contig_acres", "open_space_share", "passes_filters"])


def test_ranking(out):
    s = out.sites
    p = s[s.passes_filters.astype(bool)]
    assert sorted(p["rank"].astype(int)) == list(range(1, len(p) + 1)), "ranks of passing sites are 1..n"
    assert s[~s.passes_filters.astype(bool)]["rank"].isna().all(), "failing sites have no rank"
    sh = out.short
    assert sh["rank"].tolist() == list(range(1, len(sh) + 1)), "shortlist holds the top-n ranks in order"
    assert sh.score.is_monotonic_decreasing
    assert not (set(sh.site_type) & EXCLUDED_TYPES)


def test_areas_and_shares_are_physical(out):
    s = out.sites
    for c in ("wetland_share", "flood_share", "open_space_share", "steep_share", "forest_share_buildable", "landfill_share"):
        assert s[c].between(-1e-9, 1 + 1e-6).all(), c
    assert (s.buildable_acres <= s.parcel_acres + 1e-6).all(), "buildable > parcel"
    assert (s.largest_contig_acres <= s.buildable_acres + 1e-6).all(), "largest piece > buildable"
    assert (s.parcel_acres >= out.r.weights["project"]["min_parcel_acres"] - 1e-6).all()


def test_ids_unique_and_assemblage_members_real(out):
    sh, lots = out.short, out.lots
    assert sh.parcel_id.is_unique and sh.assemblage_id.is_unique
    assert out.sites.assemblage_id.is_unique
    pid = out.r.parcel_fields["parcel_id"]
    lot_ids = set(lots[pid].astype(str))
    members = [m for ids in sh.member_ids.astype(str) for m in ids.split("; ")]
    missing = [m for m in members if m not in lot_ids]
    assert not missing, f"member lots not in the lot table: {missing[:5]}"
    assert (sh.n_parcels == sh.member_ids.astype(str).str.count("; ") + 1).all()


def test_geometries_valid_and_inside_region(out):
    g = out.geo
    assert g.crs.to_epsg() == 4326
    assert g.geometry.is_valid.all() and not g.geometry.is_empty.any()
    towns = out.layer("towns").to_crs(4326)
    inside = g.geometry.representative_point().within(towns.union_all().buffer(0.001))
    assert inside.all(), f"{int((~inside).sum())} shortlisted sites fall outside the region's towns"
    b = gpd.read_file(out.d / "shortlist_buildable.geojson")
    assert len(b) == len(g)


def test_flags_agree_with_values(out):
    s = out.short
    fl = s.site_flags.fillna("")
    zero = s.hc_mw.notna() & (s.hc_mw <= 0)
    assert fl[zero].str.contains("0 MW headroom").all()
    assert fl[s.hc_mw.isna()].str.contains("Hosting capacity unknown").all()
    assert fl[s.site_type == "landfill"].str.contains("Landfill:").all()
    assert fl[s.site_type == "brownfield"].str.contains("Brownfield:").all()
    assert fl[s.n_parcels > 1].str.contains("Assemblage:").all()
    assert fl[s.permitting_status == "moratorium"].str.contains("moratorium", case=False).all()


def test_owner_list_reconciles_with_shortlist(out):
    o, s = out.owners, out.short
    assert o.sites.sum() == len(s)
    assert o.parcels.sum() == s.n_parcels.sum()
    assert o.est_mwac.sum() == pytest.approx(s.est_mwac.sum(), abs=0.05 * len(o))
    assert set(o.owner_type) <= {"public", "utility", "business", "trust", "individual"}
    assert o.best_rank.is_monotonic_increasing


def test_site_cards(out):
    s = out.short
    assert len(out.cards) == min(out.meta["cards"], len(s))
    for i, path in enumerate(out.cards):
        html = path.read_text(encoding="utf-8")
        assert "data:image/png;base64," in html, f"{path.name}: no map"
        row = s.iloc[i]
        assert path.name.startswith(f"card_{int(row['rank']):03d}_"), f"{path.name} out of rank order"
        assert str(row.parcel_id) in html or str(row.parcel_id).replace("_", " ") in html


# ------------------------------------------------------------------------- independent recomputation
def test_feeder_distance_recomputed_from_inputs(out):
    feeders = out.layer("feeders")
    if feeders.empty:
        pytest.skip("no feeders layer")
    s = out.short.dropna(subset=["dist_3ph_m"]).head(20)
    sites = out.sites.set_index("assemblage_id").loc[s.assemblage_id]
    dist = [feeders.distance(g).min() for g in sites.geometry]
    bad = [(a, round(b, 1), round(c, 1)) for a, b, c in zip(s.parcel_id, s.dist_3ph_m, dist) if abs(b - c) > 1.0]
    assert not bad, f"(parcel, reported m, recomputed m): {bad}"


def test_site_type_evidence_recomputed_from_inputs(out):
    """Every shortlisted landfill touches a landfill polygon/point; every brownfield has a registry point within 5 m."""
    sites = out.sites.set_index("assemblage_id")
    short = out.short
    lf = pd.concat([out.layer("landfills_poly"), out.layer("dumping_grounds_poly"), out.layer("landfills_pts")])
    bf = pd.concat([out.layer("brownfields_21e"), out.layer("brownfields_aul")])
    problems = []
    for _, r in short[short.site_type.isin(["landfill", "brownfield"])].iterrows():
        g = sites.loc[r.assemblage_id].geometry
        src = lf if r.site_type == "landfill" else bf
        tol = 0 if r.site_type == "landfill" else 5
        if src.empty or not src.intersects(g.buffer(tol)).any():
            problems.append(f"{r.parcel_id} ({r.site_type})")
    assert not problems, f"no supporting registry feature: {problems}"


def test_outputs_reproducible_from_cached_metrics(out, report):
    """Rescoring metrics.pkl with today's code, weights and tracker should reproduce scored_parcels.gpkg.
    A failure means the outputs are stale: weights, scoring code or tracker changed since the last `reservoir score`."""
    from reservoir import scoring

    mp = out.d / "metrics.pkl"
    if not mp.exists():
        pytest.skip("no metrics.pkl cache")
    df = pickle.loads(mp.read_bytes())
    new = scoring.score(out.r, df).set_index("assemblage_id")
    old = out.sites.set_index("assemblage_id")
    common = old.index.intersection(new.index)
    d_score = (new.loc[common, "score"] - old.loc[common, "score"]).abs()
    d_pass = new.loc[common, "passes_filters"].astype(bool) != old.loc[common, "passes_filters"].astype(bool)
    top_old = set(old[old["rank"] <= 50].index)
    top_new = set(new[new["rank"] <= 50].index)
    report.table(f"Outputs: reproducibility ({out.r.id})", [{
        "sites in file": len(old), "sites rescored": len(new), "score changed > 0.1": int((d_score > 0.1).sum()),
        "max score change": round(float(d_score.max() or 0), 2), "pass/fail flipped": int(d_pass.sum()),
        "top-50 overlap": f"{len(top_old & top_new)}/50"}])
    assert len(common) == len(old) == len(new)
    assert not d_pass.any() and (d_score <= 0.1).all(), (
        f"outputs are stale: {int((d_score > 0.1).sum())} scores and {int(d_pass.sum())} pass/fail results differ "
        f"from a rescore of metrics.pkl. Rerun `reservoir score --region {out.r.id}`.")


def test_summary_for_report(out, report):
    s, sh = out.sites, out.short
    report.table(f"Outputs: summary ({out.r.id})", [{
        "generated": out.meta["generated"], "lots": out.meta["lots"], "sites": len(s), "passing": int(s.passes_filters.sum()),
        "shortlist": len(sh), "score min/median/max": f"{sh.score.min():.1f} / {sh.score.median():.1f} / {sh.score.max():.1f}",
        "est. MWac (shortlist)": round(sh.est_mwac.sum(), 1), "HC unknown": int(sh.hc_mw.isna().sum()),
        "0 MW feeders": int((sh.hc_mw <= 0).sum()), "assemblages": int((sh.n_parcels > 1).sum()), "owners": len(out.owners)}])
    report.table(f"Outputs: shortlist site types ({out.r.id})",
                 [{"site_type": k, "sites": int(v)} for k, v in sh.site_type.value_counts().items()])
