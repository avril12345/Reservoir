"""Minimal ArcGIS REST client: paginated feature queries -> GeoDataFrame, with an on-disk cache.

Every vector source in this project (MassGIS, FEMA NFHL, USFWS NWI, utility hosting-capacity
portals, DOE/NETL energy communities) is an ArcGIS Feature/MapServer layer, so one well-behaved
query helper covers them all.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString, MultiLineString, MultiPoint, Point, Polygon, shape
from shapely.geometry.polygon import orient

log = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
USER_AGENT = "reservoir-screener/0.1 (open-source DG site screening)"
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def _get(url: str, params: dict, retries: int = 4, timeout: int = 90) -> dict:
    for attempt in range(retries):
        try:
            r = _session.post(url, data=params, timeout=timeout)
            r.raise_for_status()
            payload = r.json()
            if "error" in payload:
                raise RuntimeError(f"ArcGIS error from {url}: {payload['error']}")
            return payload
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            log.warning("retrying %s in %ss (%s)", url, wait, exc)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _esri_to_shapely(geom: dict | None):
    """Convert Esri JSON geometry to shapely. Outer rings are clockwise in Esri JSON."""
    if not geom:
        return None
    if "x" in geom:
        return Point(geom["x"], geom["y"])
    if "points" in geom:
        return MultiPoint(geom["points"])
    if "paths" in geom:
        paths = [p for p in geom["paths"] if len(p) >= 2]
        return LineString(paths[0]) if len(paths) == 1 else MultiLineString(paths)
    if "rings" in geom:
        shells, holes = [], []
        for ring in geom["rings"]:
            if len(ring) < 4:
                continue
            poly = Polygon(ring)
            # Esri: clockwise = exterior. shapely: exterior.is_ccw False => clockwise.
            (holes if poly.exterior.is_ccw else shells).append(poly)
        polys = []
        for s in shells:
            inner = [h.exterior.coords for h in holes if s.contains(h.representative_point())]
            polys.append(orient(Polygon(s.exterior.coords, inner)))
        if not polys:
            return None
        from shapely.ops import unary_union

        return polys[0] if len(polys) == 1 else unary_union(polys)
    return None


def layer_info(url: str) -> dict:
    return _get(url, {"f": "json"})


def query(
    url: str,
    where: str = "1=1",
    bbox: tuple[float, float, float, float] | None = None,
    bbox_crs: int = 4326,
    out_fields: str | list[str] = "*",
    out_crs: int = 4326,
    return_geometry: bool = True,
    page_size: int | None = None,
    cache: bool = True,
    max_records: int | None = None,
) -> gpd.GeoDataFrame:
    """Query an ArcGIS layer (``.../FeatureServer/0`` or ``.../MapServer/0``) with pagination.

    ``bbox`` is (xmin, ymin, xmax, ymax) in ``bbox_crs``. Results are cached as GeoParquet keyed
    on the request so reruns are reproducible and offline-capable.
    """
    if isinstance(out_fields, list):
        out_fields = ",".join(out_fields)
    key = hashlib.sha1(json.dumps([url, where, bbox, bbox_crs, out_fields, out_crs, return_geometry]).encode()).hexdigest()[:16]
    cache_path = CACHE_DIR / f"{key}.parquet"
    if cache and cache_path.exists():
        return gpd.read_parquet(cache_path) if return_geometry else pd.read_parquet(cache_path)

    info = layer_info(url)
    page_size = page_size or min(int(info.get("maxRecordCount") or 1000), 2000)
    params = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": str(return_geometry).lower(),
        "outSR": out_crs,
        "f": "json",
        "resultRecordCount": page_size,
    }
    if bbox is not None:
        params.update(
            geometry=",".join(map(str, bbox)),
            geometryType="esriGeometryEnvelope",
            inSR=bbox_crs,
            spatialRel="esriSpatialRelIntersects",
        )

    rows, geoms, offset = [], [], 0
    while True:
        payload = _get(f"{url}/query", {**params, "resultOffset": offset})
        feats = payload.get("features", [])
        for f in feats:
            rows.append(f.get("attributes", {}))
            geoms.append(_esri_to_shapely(f.get("geometry")) if return_geometry else None)
        offset += len(feats)
        if max_records and offset >= max_records:
            break
        if not feats or not (payload.get("exceededTransferLimit") or len(feats) == page_size):
            break
    df = pd.DataFrame(rows)
    if return_geometry:
        out = gpd.GeoDataFrame(df, geometry=geoms, crs=f"EPSG:{out_crs}")
        out = out[out.geometry.notna()].reset_index(drop=True)
    else:
        out = df
    if cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out.to_parquet(cache_path)
    log.info("fetched %d features from %s", len(out), url)
    return out


def from_geojson(features: list[dict], crs: int = 4326) -> gpd.GeoDataFrame:
    """Helper for tests/manual imports."""
    return gpd.GeoDataFrame(
        [f.get("properties", {}) for f in features],
        geometry=[shape(f["geometry"]) for f in features],
        crs=f"EPSG:{crs}",
    )
