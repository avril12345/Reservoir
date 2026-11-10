"""Site geometry metrics (a site is one lot or an assemblage of same-owner neighbours): buildable area, site type, overlays."""
from __future__ import annotations

import json
import logging
import re

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import make_valid
from shapely.ops import unary_union

from . import grid, rasters
from .bylaws import tracker
from .config import Region
from .fetch import ACRE_M2

log = logging.getLogger(__name__)
FT_M = 0.3048


# ---------------------------------------------------------------- property-line setback (town bylaw or default)
def property_line_m(region: Region, town, records: dict) -> tuple[float, str, float]:
    """(metres, source, feet) of the solar yard setback for a site in `town`. Uses the tracker's
    `solar_setback_ft` only when it is stored, i.e. extracted with a verified quote or entered by a reviewer;
    anything else (no record, unknown pathway, failed quote check) falls back to `setbacks_m.property_line`."""
    rec = records.get(tracker.key(region.state, str(town)))
    if rec is not None and rec.solar_setback_ft is not None:
        return rec.solar_setback_ft * FT_M, "tracker", float(rec.solar_setback_ft)
    m = float(region.weights["setbacks_m"]["property_line"])
    return m, "default", m / FT_M


def property_setbacks(region: Region, sites: gpd.GeoDataFrame, records: dict | None = None) -> pd.DataFrame:
    """Per-site setback columns: setback_m (used for the buffer), setback_ft_applied, setback_source."""
    records = tracker.load() if records is None else records
    vals = [property_line_m(region, t, records) for t in sites["_town"]]
    return pd.DataFrame(vals, index=sites.index, columns=["setback_m", "setback_source", "setback_ft_applied"])


def setback_flags(region: Region, town, applied_ft: float, source: str, records: dict) -> list[str]:
    """Flags explaining the setback used for geometry, and the setbacks deliberately not applied to it."""
    rec = records.get(tracker.key(region.state, str(town)))
    if source == "tracker":
        flags = [f"Property-line setback {applied_ft:g} ft from siting tracker"]
    else:
        why = "town bylaw not in tracker" if rec is None else "tracker has no solar setback for this town"
        flags = [f"Property-line setback {applied_ft * FT_M:g} m (~{applied_ft:.0f} ft default); {why}"]
    _, now_source, now_ft = property_line_m(region, town, records)
    if now_source != source or abs(now_ft - applied_ft) > 0.01:
        flags.append(f"Tracker now gives {now_ft:.0f} ft but geometry used {applied_ft:.0f} ft. Rerun `reservoir score --reanalyse`")
    if rec is None:
        return flags
    if rec.from_residence_ft is not None:
        flags.append(f"Bylaw sets {rec.from_residence_ft:g} ft from residences; not applied to geometry (no residences layer)")
    if rec.bess_setback_ft is not None and rec.bess_setback_ft > applied_ft:
        flags.append(f"BESS setback {rec.bess_setback_ft:g} ft exceeds the {applied_ft:.0f} ft solar setback; storage siting may need more yard")
    return flags


def read_inputs(region: Region) -> dict[str, gpd.GeoDataFrame]:
    import pyogrio

    path = region.out_dir / "inputs.gpkg"
    return {name: gpd.read_file(path, layer=name) for name, _ in pyogrio.list_layers(path)}


# ---------------------------------------------------------------- site type from assessor use codes
def site_type_from_use(state: str, code) -> str:
    digits = re.sub(r"\D", "", str(code or ""))
    if len(digits) < 3:
        return "other"
    c = int(digits[:3])
    if state == "MA":  # MA DOR property type classification codes
        if 410 <= c <= 419:
            return "gravel_pit"          # 410 sand & gravel, 411 rock/stone quarry, 412 other mining
        if 440 <= c <= 442:
            return "industrial_vacant"   # developable / potentially developable / undevelopable industrial land
        if 420 <= c <= 429:
            return "utility"             # 423 generation, 424 electric ROW, ...
        if 400 <= c <= 409 or 430 <= c <= 439 or 450 <= c <= 452:
            return "industrial"
        if 390 <= c <= 392:
            return "vacant_other"        # developable commercial land
        if 300 <= c <= 389:
            return "commercial"
        if 700 <= c <= 729:
            return "agricultural"        # Chapter 61A
        if 130 <= c <= 132:
            return "vacant_other"        # developable residential land
        if 100 <= c <= 199 or c in (13, 31):
            return "residential"
        if 900 <= c <= 999:
            return "municipal_exempt" if c in (903, 930, 931, 932) else "other"
        return "other"
    if state == "NY":  # NY ORPS property class
        if c == 852:
            return "landfill"
        if 720 <= c <= 729:
            return "gravel_pit"
        if c == 340:
            return "industrial_vacant"
        if 700 <= c <= 799:
            return "industrial"
        if 800 <= c <= 899:
            return "utility"
        if 400 <= c <= 499:
            return "commercial"
        if 300 <= c <= 399:
            return "vacant_other"
        if 100 <= c <= 199:
            return "agricultural"
        if 200 <= c <= 299:
            return "residential"
    return "other"


def _union(gdf: gpd.GeoDataFrame | None, buffer_m: float = 0.0):
    if gdf is None or gdf.empty:
        return None
    g = gdf.geometry.make_valid()
    g = g[g.geom_type.isin(["Polygon", "MultiPolygon", "GeometryCollection"])]
    if buffer_m:
        g = g.buffer(buffer_m)
    return unary_union(g.values) if len(g) else None


def _clip_union(layer: gpd.GeoDataFrame, sindex_geom, buffer_m=0.0):
    """Union of only the layer features that touch `sindex_geom` (fast per-parcel clip)."""
    if layer is None or layer.empty:
        return None
    idx = layer.sindex.query(sindex_geom.buffer(buffer_m) if buffer_m else sindex_geom, predicate="intersects")
    if len(idx) == 0:
        return None
    return _union(layer.iloc[idx], buffer_m)


# ---------------------------------------------------------------- assemblages (same-owner neighbouring lots)
def _norm(s) -> str:
    s = str(s or "").upper()
    s = "" if s in ("NONE", "NAN") else s
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", "", s)).strip()


def owner_key(name, address) -> str:
    """Owner name + mailing address, uppercased, punctuation stripped. Empty unless both are present:
    name alone would glue every TOWN OF UXBRIDGE lot into one site."""
    n, a = _norm(name), _norm(address)
    if n in _PLACEHOLDER_OWNERS or a in _PLACEHOLDER_OWNERS:
        return ""
    return f"{n}|{a}" if n and a else ""


_PLACEHOLDER_OWNERS = {"UNKNOWN", "OWNER UNKNOWN", "UNKNOWN OWNER", "OWNERS UNKNOWN", "NONE", "NA", "N A", "TBD", "OWNER", "UNK"}


def min_lot_acres(region: Region) -> float:
    """Smallest lot fetched/analysed: below min_parcel_acres when assemblages are on, since small lots can add up."""
    pj, asm = region.weights["project"], region.weights.get("assemblage", {})
    return asm.get("min_lot_acres", pj["min_parcel_acres"]) if asm.get("enabled") else pj["min_parcel_acres"]


class _UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _road_between(a, b, roads: gpd.GeoDataFrame, gap_m: float, min_len_m: float = 5.0) -> bool:
    """True if a mapped (built) road runs through the space between lots a and b, or along their shared line
    (towns that draw parcels to the road centreline). The space between is filled by a morphological
    closing, which bridges the gap without reaching out over a road that merely runs past both fronts."""
    both = unary_union([a, b])
    r = gap_m / 2 + 0.5
    zone = both.buffer(r).buffer(-r).difference(both).union(a.intersection(b).buffer(1.0))
    idx = roads.sindex.query(zone, predicate="intersects")
    return bool(len(idx)) and roads.geometry.iloc[idx].intersection(zone).length.sum() >= min_len_m


def assemble(lots: gpd.GeoDataFrame, max_gap_m: float, roads: gpd.GeoDataFrame | None = None,
             touch_m: float = 1.0) -> pd.DataFrame:
    """Group lots into assemblages: connected components of the graph whose edges join two eligible lots
    with the same non-empty `owner_key`, in the same `_town`, at most `max_gap_m` apart, with no mapped road
    between them (a public way makes two sites; an unbuilt paper road isn't in a road inventory). Ineligible
    lots (residential, excluded use, conservation) and lots with no owner key stay single.

    Returns `assemblage_id` (ids 0..n-1, in lot order) and `gap_joined`: True when the assemblage only
    holds together through a gap wider than `touch_m`, which is worth a look on imagery."""
    uf, touch = _UnionFind(lots.index), _UnionFind(lots.index)
    has_roads = roads is not None and len(roads) > 0
    ok = lots[lots["eligible"] & lots["owner_key"].ne("")]
    if len(ok) > 1:
        pairs = gpd.sjoin(ok[["geometry"]], ok[["geometry"]], predicate="dwithin", distance=max_gap_m)
        pairs = pairs[pairs.index < pairs["index_right"]]
        for a, b in zip(pairs.index, pairs["index_right"]):
            if lots.at[a, "owner_key"] != lots.at[b, "owner_key"] or lots.at[a, "_town"] != lots.at[b, "_town"]:
                continue
            if has_roads and _road_between(lots.geometry[a], lots.geometry[b], roads, max_gap_m):
                continue
            uf.union(a, b)
            if lots.geometry[a].distance(lots.geometry[b]) <= touch_m:
                touch.union(a, b)
    root = pd.Series({i: uf.find(i) for i in lots.index})
    ids = {r: n for n, r in enumerate(dict.fromkeys(root))}
    out = pd.DataFrame({"assemblage_id": root.map(ids)}, index=lots.index)
    touch_root = pd.Series({i: touch.find(i) for i in lots.index})
    out["gap_joined"] = touch_root.groupby(out["assemblage_id"]).transform("nunique") > 1
    return out


def prepare_lots(region: Region, parcels: gpd.GeoDataFrame, roads: gpd.GeoDataFrame | None = None) -> gpd.GeoDataFrame:
    """Per-lot attributes needed before dissolving: use type, owner key, and whether the lot may join a site."""
    w, pf = region.weights, region.parcel_fields
    lots = parcels.copy()
    val = lambda f: lots[pf[f]].fillna("") if pf[f] and pf[f] in lots else pd.Series("", index=lots.index)
    lots["use_type"] = val("use_code").map(lambda c: site_type_from_use(region.state, c))
    desc = (val("use_desc").astype(str) + " " + val("owner_name").astype(str)).str.upper()
    lots["conservation_use"] = desc.str.contains("CONSERVATION")
    kws = w["project"].get("exclude_use_keywords", [])
    lots["excluded_keyword"] = desc.map(lambda d: next((k for k in kws if k in d), ""))
    lots["owner_key"] = [owner_key(n, a) for n, a in zip(val("owner_name"), val("owner_address"))]
    # A residential lot glued to a gravel pit must not turn into a gravel-pit site: such lots stay single.
    lots["eligible"] = lots.use_type.ne("residential") & lots.excluded_keyword.eq("") & ~lots.conservation_use
    asm = w.get("assemblage", {})
    if asm.get("enabled"):
        lots = lots.join(assemble(lots, asm.get("max_gap_m", 20), roads))
    else:
        lots["assemblage_id"], lots["gap_joined"] = range(len(lots)), False
    return lots


def build_sites(region: Region, lots: gpd.GeoDataFrame, snap_m: float = 1.0) -> gpd.GeoDataFrame:
    """One row per assemblage. Attributes come from the largest lot (so owner/address/use columns keep their
    source names); geometry is the dissolved union, with sub-metre digitising slivers closed so interior lot
    lines really disappear before the property-line setback is applied."""
    sts = region.weights["site_type_scores"]
    pid = region.parcel_fields["parcel_id"]
    rows = []
    for aid, g in lots.sort_values("parcel_acres", ascending=False).groupby("assemblage_id", sort=False):
        rep = g.iloc[0].copy()
        n = len(g)
        if n > 1:
            rep["geometry"] = make_valid(unary_union([x.buffer(snap_m) for x in g.geometry]).buffer(-snap_m))
            rep["parcel_acres"] = g.parcel_acres.sum()
            # Members are all eligible, so precedence among them is simply the best-scoring use.
            rep["use_type"] = max(g.use_type, key=lambda t: sts.get(t, sts["other"]))
        ids = g[pid].astype(str).tolist() if pid in g else [str(i) for i in g.index]
        rep["n_parcels"] = n
        rep["member_ids"] = "; ".join(ids)
        rep["lots_summary"] = f"{n} lots, same owner, {rep['parcel_acres']:.1f} ac combined" if n > 1 else ""
        rows.append(rep)
    sites = gpd.GeoDataFrame(rows, geometry="geometry", crs=lots.crs).reset_index(drop=True)
    sites["n_parcels"] = sites["n_parcels"].astype(int)
    return sites


def analyse(region: Region, progress=print) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Returns (sites, lots). Sites are the scoring grain: a single lot, or a dissolved assemblage of
    same-owner neighbouring lots. Lots keep every tax-lot row, tagged with its assemblage_id."""
    L = read_inputs(region)
    w = region.weights
    sb = w["setbacks_m"]
    crs = region.crs
    parcels = L["parcels"].copy()
    parcels["geometry"] = parcels.geometry.make_valid()
    parcels["parcel_acres"] = parcels.area / ACRE_M2
    parcels = parcels[parcels.parcel_acres >= min_lot_acres(region)].reset_index(drop=True)
    lots = prepare_lots(region, parcels, L.get("roads"))
    sites = build_sites(region, lots)
    multi = sites[sites.n_parcels > 1]
    progress(f"{len(lots)} lots >= {min_lot_acres(region)} ac -> {len(sites)} sites "
             f"({len(multi)} assemblages of {int(multi.n_parcels.sum())} lots)")
    sites = sites[sites.parcel_acres >= w["project"]["min_parcel_acres"]].reset_index(drop=True)
    progress(f"{len(sites)} sites >= {w['project']['min_parcel_acres']} ac")
    members = lots.groupby("assemblage_id").geometry.apply(list)
    # Town setback is applied to the dissolved site (after build_sites), never to each tax lot.
    parcels = sites.join(property_setbacks(region, sites, tracker.load()))
    used = parcels.groupby(["_town", "setback_source"]).setback_ft_applied.first()
    progress("property-line setbacks: " + ", ".join(f"{t} {ft:.0f} ft ({src})" for (t, src), ft in used.items()))

    # Raster-derived constraint polygons, per town.
    raster_idx = json.loads((region.out_dir / "rasters.json").read_text())
    steep, forest, barren = [], [], []
    lc = region.national["landcover_nlcd"]
    for town, paths in raster_idx.items():
        progress(f"[{town}] slope > {sb['slope_max_pct']}% and land-cover polygons")
        steep.append(rasters.steep_polygons(paths["dem"], sb["slope_max_pct"]).to_crs(crs))
        forest.append(rasters.landcover_polygons(paths["nlcd"], lc["forest_classes"]).to_crs(crs))
        barren.append(rasters.landcover_polygons(paths["nlcd"], lc["barren_classes"]).to_crs(crs))
    as_gdf = lambda parts: gpd.GeoDataFrame(geometry=pd.concat(parts, ignore_index=True), crs=crs) if parts else gpd.GeoDataFrame(geometry=[], crs=crs)
    steep, forest, barren = as_gdf(steep), as_gdf(forest), as_gdf(barren)

    get = lambda k: L.get(k, gpd.GeoDataFrame(geometry=[], crs=crs))
    wet, flood, osp, bldg = get("wetlands"), get("flood"), get("open_space"), get("buildings")
    landfill_poly = pd.concat([get("landfills_poly"), get("dumping_grounds_poly")], ignore_index=True)
    landfill_poly = gpd.GeoDataFrame(landfill_poly, geometry="geometry", crs=crs)
    points = {"brownfield_21e": get("brownfields_21e"), "brownfield_aul": get("brownfields_aul"), "landfill_pt": get("landfills_pts")}
    low_inc, ec = get("low_income"), get("energy_community")

    rows = []
    for i, p in parcels.iterrows():
        if i % 200 == 0:
            progress(f"constraints: site {i}/{len(parcels)}")
        g = p.geometry
        area = g.area
        wet_u = _clip_union(wet, g, sb["wetland_buffer"])
        wet_raw = _clip_union(wet, g)
        fl_u, os_u, st_u = _clip_union(flood, g), _clip_union(osp, g), _clip_union(steep, g)
        bl_u = _clip_union(bldg, g, sb.get("building_buffer", 10))
        parts = [x for x in (wet_u, fl_u, os_u, st_u, bl_u) if x is not None]
        excl = unary_union(parts) if parts else None
        core = g.buffer(-p.setback_m)
        buildable = core.difference(excl) if excl is not None and not core.is_empty else core
        buildable = make_valid(buildable)
        pieces = [x for x in getattr(buildable, "geoms", [buildable]) if x.geom_type == "Polygon" and not x.is_empty]
        largest = max((x.area for x in pieces), default=0.0)
        fr_u = _clip_union(forest, g)
        ba_u = _clip_union(barren, g)
        share = lambda u, base=g: (u.intersection(base).area / base.area) if (u is not None and base.area > 0) else 0.0

        # Site-type evidence
        lf_u = _clip_union(landfill_poly, g)
        lf_share = share(lf_u)
        # An assemblage is a landfill if any member lot is (a big clean lot must not dilute a capped one below 10%).
        lf_type_share = max([lf_share, *(share(lf_u, m) for m in members[p.assemblage_id])]) if p.n_parcels > 1 else lf_share
        # Registry points are address-geocoded; a 5 m tolerance keeps them from bleeding into neighbours.
        hits = {k: v.iloc[v.sindex.query(g.buffer(5), predicate="intersects")] if len(v) else v for k, v in points.items()}
        lab = lambda df, cols: "; ".join(" ".join(str(r[c]) for c in cols if c in df and str(r[c]) not in ("None", "nan")) for _, r in df.head(3).iterrows())
        use_type, conservation_use, excluded_kw = p.use_type, p.conservation_use, p.excluded_keyword
        barren_share = share(ba_u)
        # Precedence: uses that can't host a project win over registry evidence (a school on a closed 21E
        # site is still a school); a landfill on "conservation" land stays a landfill but gets an Article 97 flag.
        if use_type == "residential":
            site_type = "residential"     # e.g. condos on a former mill site: registry hit, but not a target
        elif excluded_kw:
            site_type = "excluded_use"
        elif lf_type_share >= 0.10 or len(hits["landfill_pt"]):
            site_type = "landfill"
        elif len(hits["brownfield_21e"]) or len(hits["brownfield_aul"]):
            site_type = "brownfield"
        elif use_type == "gravel_pit":
            site_type = "gravel_pit"
        elif barren_share >= 0.30 and use_type in ("vacant_other", "industrial_vacant", "other", "agricultural", "municipal_exempt"):
            site_type = "disturbed_land"  # NLCD barren on vacant land: often an unrecorded pit, but also graded lots
        elif conservation_use:
            site_type = "conservation"    # municipal conservation land: Article 97 protection
        else:
            site_type = use_type

        rows.append({
            "buildable_geom": buildable,
            "buildable_acres": buildable.area / ACRE_M2 if not buildable.is_empty else 0.0,
            "largest_contig_acres": largest / ACRE_M2,
            "wetland_share": share(wet_raw),
            "flood_share": share(fl_u),
            "open_space_share": share(os_u),
            "steep_share": share(st_u),
            "building_share": share(bl_u),
            "forest_share_parcel": share(fr_u),
            "forest_share_buildable": share(fr_u, buildable) if not buildable.is_empty else 0.0,
            "barren_share": barren_share,
            "landfill_share": lf_share,
            "site_type": site_type,
            "brownfield_rtns": lab(pd.concat([hits["brownfield_21e"], hits["brownfield_aul"]]), ["RTN", "NAME", "STATUS", "RAO_CLASS"]),
            "landfill_label": lab(landfill_poly.iloc[landfill_poly.sindex.query(g, predicate="intersects")] if len(landfill_poly) else landfill_poly, ["SITE_NAME", "STATUS", "CAP_STATUS"]) or lab(hits["landfill_pt"], ["SITE_NAME", "STATUS", "CAP_STATUS"]),
            "low_income": bool(len(low_inc) and len(low_inc.sindex.query(g, predicate="intersects"))),
            "energy_community": bool(len(ec) and len(ec.sindex.query(g, predicate="intersects"))),
        })
    m = pd.DataFrame(rows, index=parcels.index)
    out = parcels.join(m)
    progress("grid: nearest 3-phase feeder and substation")
    hc = region.hosting() or {}
    out = out.join(grid.nearest_feeder(out, get("feeders")))
    out = out.join(grid.nearest_substation(out, get("substations"), hc.get("substation_name")))
    return gpd.GeoDataFrame(out, geometry="geometry", crs=crs), lots
