"""Per-parcel geometry metrics: buildable area after constraints, site type, overlay flags."""
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
from .config import Region
from .fetch import ACRE_M2

log = logging.getLogger(__name__)


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


def analyse(region: Region, progress=print) -> gpd.GeoDataFrame:
    L = read_inputs(region)
    w = region.weights
    sb = w["setbacks_m"]
    crs = region.crs
    pcfg = region.sources["parcels"]
    parcels = L["parcels"].copy()
    parcels["geometry"] = parcels.geometry.make_valid()
    parcels["parcel_acres"] = parcels.area / ACRE_M2
    parcels = parcels[parcels.parcel_acres >= w["project"]["min_parcel_acres"]].reset_index(drop=True)
    progress(f"{len(parcels)} parcels >= {w['project']['min_parcel_acres']} ac")

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
            progress(f"constraints: parcel {i}/{len(parcels)}")
        g = p.geometry
        area = g.area
        wet_u = _clip_union(wet, g, sb["wetland_buffer"])
        wet_raw = _clip_union(wet, g)
        fl_u, os_u, st_u = _clip_union(flood, g), _clip_union(osp, g), _clip_union(steep, g)
        bl_u = _clip_union(bldg, g, sb.get("building_buffer", 10))
        parts = [x for x in (wet_u, fl_u, os_u, st_u, bl_u) if x is not None]
        excl = unary_union(parts) if parts else None
        core = g.buffer(-sb["property_line"])
        buildable = core.difference(excl) if excl is not None and not core.is_empty else core
        buildable = make_valid(buildable)
        pieces = [x for x in getattr(buildable, "geoms", [buildable]) if x.geom_type == "Polygon" and not x.is_empty]
        largest = max((x.area for x in pieces), default=0.0)
        fr_u = _clip_union(forest, g)
        ba_u = _clip_union(barren, g)
        share = lambda u, base=g: (u.intersection(base).area / base.area) if (u is not None and base.area > 0) else 0.0

        # Site-type evidence
        lf_share = share(_clip_union(landfill_poly, g))
        # Registry points are address-geocoded; a 5 m tolerance keeps them from bleeding into neighbours.
        hits = {k: v.iloc[v.sindex.query(g.buffer(5), predicate="intersects")] if len(v) else v for k, v in points.items()}
        lab = lambda df, cols: "; ".join(" ".join(str(r[c]) for c in cols if c in df and str(r[c]) not in ("None", "nan")) for _, r in df.head(3).iterrows())
        use_type = site_type_from_use(region.state, p.get(pcfg["use_code_field"]))
        desc = f'{p.get(pcfg.get("use_desc_field"), "")} {p.get(pcfg["owner_fields"]["name"], "")}'.upper()
        conservation_use = "CONSERVATION" in desc
        excluded_kw = next((k for k in w["project"].get("exclude_use_keywords", []) if k in desc), "")
        barren_share = share(ba_u)
        # Precedence: uses that can't host a project win over registry evidence (a school on a closed 21E
        # site is still a school); a landfill on "conservation" land stays a landfill but gets an Article 97 flag.
        if use_type == "residential":
            site_type = "residential"     # e.g. condos on a former mill site: registry hit, but not a target
        elif excluded_kw:
            site_type = "excluded_use"
        elif lf_share >= 0.10 or len(hits["landfill_pt"]):
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
            "conservation_use": conservation_use,
            "excluded_keyword": excluded_kw,
            "forest_share_parcel": share(fr_u),
            "forest_share_buildable": share(fr_u, buildable) if not buildable.is_empty else 0.0,
            "barren_share": barren_share,
            "landfill_share": lf_share,
            "site_type": site_type,
            "use_type": use_type,
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
    return gpd.GeoDataFrame(out, geometry="geometry", crs=crs)
