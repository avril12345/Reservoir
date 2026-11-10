"""Fetch every input layer for a region, one town at a time, into a single GeoPackage.

Output: outputs/<region>/inputs.gpkg with one layer per source (all in the region's projected CRS),
plus per-town raster paths in outputs/<region>/rasters.json. Everything is cached under data/cache,
so a rerun is offline and bit-for-bit reproducible.
"""
from __future__ import annotations

import json
import logging

import geopandas as gpd
import pandas as pd
from pyproj import Transformer

from . import arcgis, grid, rasters
from .config import Region

log = logging.getLogger(__name__)
ACRE_M2 = 4046.8564224


def resolve_towns(region: Region) -> gpd.GeoDataFrame:
    """Town polygons for the region (MA: from the Electricity Providers layer, which carries utility)."""
    src = region.sources.get("utility_territory") or region.sources["towns"]
    name_f = src.get("town_field") or src.get("name_field")
    if region.all_utility_towns and "utility_field" in src:
        where = f"{src['utility_field']} LIKE '%{region.utility}%'"
    else:
        names = ",".join(f"'{t}'" for t in region.towns)
        where = f"UPPER({name_f}) IN ({names})"
    towns = arcgis.query(src["url"], where=where, out_crs=region.crs)
    if towns.empty:
        raise ValueError(f"No towns matched for region {region.id}: {where}")
    towns = towns.rename(columns={name_f: "town", src["id_field"]: "town_id"})
    if "utility_field" in src:
        towns = towns.rename(columns={src["utility_field"]: "utility"})
    towns["town"] = towns["town"].str.upper()
    return towns.dissolve(by=["town", "town_id"], as_index=False, aggfunc="first")


def _bbox4326(geom, crs: int, pad_m: float = 500) -> tuple:
    xmin, ymin, xmax, ymax = geom.buffer(pad_m).bounds
    t = Transformer.from_crs(crs, 4326, always_xy=True)
    xs, ys = t.transform([xmin, xmax, xmin, xmax], [ymin, ymin, ymax, ymax])
    return (min(xs), min(ys), max(xs), max(ys))


def _layer(url: str | None, bbox, crs: int, where: str = "1=1", fields="*") -> gpd.GeoDataFrame:
    if not url:
        return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{crs}")
    try:
        return arcgis.query(url, where=where, bbox=bbox, out_fields=fields, out_crs=crs)
    except Exception as exc:  # one flaky public server should not kill a territory run
        log.error("layer failed %s: %s", url, exc)
        return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{crs}")


def fetch_region(region: Region, progress=print) -> dict:
    s, n, crs = region.sources, region.national, region.crs
    out = region.out_dir / "inputs.gpkg"
    towns = resolve_towns(region)
    from .analysis import min_lot_acres

    min_acres = min_lot_acres(region)
    hc = region.hosting() or {}
    layers: dict[str, list[gpd.GeoDataFrame]] = {}
    raster_index = {}

    def add(name, gdf, town):
        if gdf is not None and len(gdf):
            gdf = gdf.copy()
            gdf["_town"] = town
            layers.setdefault(name, []).append(gdf)

    for _, t in towns.iterrows():
        town, geom = t["town"], t.geometry
        bbox = _bbox4326(geom, crs)
        progress(f"[{town}] parcels")
        p = s["parcels"]
        # Shape__Area is web-mercator m^2 (~1.8x true area at 42°N); 1.5x keeps a safety margin.
        where = f"{p['town_id_field']} = {int(t['town_id'])} AND Shape__Area > {min_acres * ACRE_M2 * 1.5:.0f}"
        add("parcels", _layer(p["url"], None, crs, where=where, fields=p.get("fields", "*")), town)

        progress(f"[{town}] constraints: wetlands, flood, open space")
        add("wetlands", _layer(n["wetlands_nwi"]["url"], bbox, crs), town)
        add("wetlands", _layer(s.get("wetlands_state", {}).get("url"), bbox, crs), town)
        zones = ",".join(f"'{z}'" for z in n["flood_nfhl"]["exclude_zones"])
        add("flood", _layer(n["flood_nfhl"]["url"], bbox, crs, where=f"FLD_ZONE IN ({zones})"), town)
        os_cfg = s.get("protected_open_space", {})
        add("open_space", _layer(os_cfg.get("url"), bbox, crs, where=os_cfg.get("exclude_where", "1=1")), town)
        b = s.get("buildings", {})
        if b.get("url"):
            add("buildings", _layer(b["url"], None, crs, where=b["town_where"].format(town_id=int(t["town_id"])), fields=b.get("fields", "*")), town)

        rd = s.get("roads", {})
        add("roads", _layer(rd.get("url"), bbox, crs, fields=rd.get("fields", "*")), town)

        progress(f"[{town}] site-type overlays: 21E, AUL, landfills")
        for key in ("brownfields_21e", "brownfields_aul", "landfills_poly", "landfills_pts", "dumping_grounds_poly"):
            add(key, _layer(s.get(key, {}).get("url"), bbox, crs), town)

        progress(f"[{town}] incentives: EJ / energy community")
        ej = s.get("low_income_ej", {})
        add("low_income", _layer(ej.get("url"), bbox, crs, where=ej.get("income_where", "1=1")), town)
        add("energy_community", _layer(n["energy_community_msa"]["url"], bbox, crs), town)
        add("energy_community", _layer(n["energy_community_coal"]["url"], bbox, crs), town)

        progress(f"[{town}] grid: 3-phase feeders + hosting capacity, substations")
        try:
            add("feeders", grid.fetch_feeders(hc, geom.buffer(1500).bounds, crs), town)
        except Exception as exc:
            log.error("feeders failed for %s: %s", town, exc)
        add("substations", _layer(hc.get("substations_url"), bbox, crs), town)

        progress(f"[{town}] rasters: 3DEP slope, NLCD land cover")
        tb = geom.buffer(200).bounds
        raster_index[town] = {
            "dem": str(rasters.fetch_dem(n["dem_3dep"]["url"], tb, crs, n["dem_3dep"]["resolution_m"])),
            "nlcd": str(rasters.fetch_nlcd(n["landcover_nlcd"]["wcs"], n["landcover_nlcd"]["coverage"], tb, crs)),
        }

    if out.exists():
        out.unlink()
    towns.to_file(out, layer="towns", driver="GPKG")
    counts = {"towns": len(towns)}
    for name, parts in layers.items():
        gdf = pd.concat(parts, ignore_index=True)
        # Sources merged into one layer (e.g. NWI + MassDEP wetlands) can carry columns that differ
        # only by case (GlobalID/GLOBALID), which GPKG rejects. Keep the first.
        gdf = gdf.loc[:, ~gdf.columns.str.lower().duplicated()]
        gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=f"EPSG:{crs}")
        gdf = gdf[~gdf.geometry.is_empty]
        # Same feature returned for two neighbouring town bboxes -> drop duplicates by geometry.
        gdf = gdf.loc[~gdf.geometry.to_wkb().duplicated()]
        # GPKG cannot store some ArcGIS dtypes cleanly; stringify object columns.
        for c in gdf.columns:
            if c != "geometry" and gdf[c].dtype == object:
                gdf[c] = gdf[c].astype(str)
        gdf.to_file(out, layer=name, driver="GPKG")
        counts[name] = len(gdf)
    (region.out_dir / "rasters.json").write_text(json.dumps(raster_index, indent=2))
    (region.out_dir / "fetch_summary.json").write_text(json.dumps(counts, indent=2))
    return counts
