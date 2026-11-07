"""Three-phase feeders + hosting capacity from utility portals.

Most utility hosting-capacity portals are ArcGIS MapServers. Some (National Grid's MA/NY System Data
Portals) return feeder *attributes* from /query but withhold vector geometry. For those we
reconstruct each feeder's path by exporting a map image filtered to that one feeder (layerDefs) and
taking the drawn pixels as the feeder's location. At 5 m/pixel with ~5 px symbology the positional
error is roughly +/-12 m, which is fine for screening (we report distance to the nearest drawn pixel).

Users are responsible for complying with each portal's terms of use.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from PIL import Image

from . import arcgis
from .arcgis import CACHE_DIR, USER_AGENT

log = logging.getLogger(__name__)


def _export_mask(map_export_url: str, layer_id: int, bbox: tuple, crs: int, res_m: float, layer_def: str | None):
    key = hashlib.sha1(repr((map_export_url, layer_id, bbox, crs, res_m, layer_def)).encode()).hexdigest()[:16]
    path = CACHE_DIR / f"{key}.png"
    if path.exists():
        content = path.read_bytes()
    else:
        xmin, ymin, xmax, ymax = bbox
        w, h = int((xmax - xmin) / res_m), int((ymax - ymin) / res_m)
        params = {
            "bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": crs, "imageSR": crs, "size": f"{w},{h}",
            "layers": f"show:{layer_id}", "format": "png32", "transparent": "true", "dpi": 96, "f": "image",
        }
        if layer_def:
            params["layerDefs"] = json.dumps({str(layer_id): layer_def})
        r = requests.get(map_export_url, params=params, headers={"User-Agent": USER_AGENT}, timeout=180)
        r.raise_for_status()
        content = r.content
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    img = np.array(Image.open(io.BytesIO(content)).convert("RGBA"))
    return img[..., 3] > 0


def _mask_points(mask: np.ndarray, bbox: tuple, res_m: float, stride: int = 2):
    rows, cols = np.nonzero(mask[::stride, ::stride])
    xs = bbox[0] + (cols * stride + 0.5) * res_m
    ys = bbox[3] - (rows * stride + 0.5) * res_m
    return xs, ys


def fetch_feeders(hc: dict, bbox: tuple, crs: int, res_m: float = 5.0) -> gpd.GeoDataFrame:
    """Return 3-phase feeder geometry (lines, or pixel points when the portal withholds geometry)
    with normalised columns: feeder_id, substation, voltage_kv, hc_mw, hc_mw_alt, updated + extras."""
    url = hc.get("feeders_url")
    if not url:
        if hc.get("feeders_file"):
            return normalise(gpd.read_file(hc["feeders_file"]).to_crs(crs), hc)
        return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{crs}")
    attrs = arcgis.query(url, bbox=bbox, bbox_crs=crs, out_crs=crs, return_geometry=False)
    if attrs.empty:
        return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{crs}")
    with_geom = arcgis.query(url, bbox=bbox, bbox_crs=crs, out_crs=crs, return_geometry=True)
    if len(with_geom):
        return normalise(with_geom, hc)

    # Geometry withheld -> reconstruct from per-feeder map exports.
    base, layer_id = url.rsplit("/", 1)
    fid = hc["feeder_id"]
    parts = []
    for _, row in attrs.drop_duplicates(fid).iterrows():
        mask = _export_mask(f"{base}/export", int(layer_id), bbox, crs, res_m, f"{fid}='{row[fid]}'")
        xs, ys = _mask_points(mask, bbox, res_m)
        if len(xs) == 0:
            continue
        pts = gpd.GeoDataFrame({k: [v] * len(xs) for k, v in row.items()}, geometry=gpd.points_from_xy(xs, ys), crs=f"EPSG:{crs}")
        parts.append(pts)
    if not parts:
        return gpd.GeoDataFrame(geometry=[], crs=f"EPSG:{crs}")
    out = normalise(gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=f"EPSG:{crs}"), hc)
    out["geom_source"] = "map_export_pixels"
    return out


def normalise(gdf: gpd.GeoDataFrame, hc: dict) -> gpd.GeoDataFrame:
    mapping = {hc.get(k): k for k in ("feeder_id", "substation", "voltage_kv", "hc_mw", "hc_mw_alt", "updated") if hc.get(k)}
    out = gdf.rename(columns={src: dst for src, dst in mapping.items() if src in gdf.columns})
    for col in ("hc_mw", "hc_mw_alt", "voltage_kv"):
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "updated" in out and pd.api.types.is_numeric_dtype(out["updated"]):
        out["updated"] = pd.to_datetime(out["updated"], unit="ms", errors="coerce").dt.date.astype(str)
    return out


def nearest_feeder(parcels: gpd.GeoDataFrame, feeders: gpd.GeoDataFrame, max_m: float = 5000) -> pd.DataFrame:
    """Distance (m) from each parcel to the nearest 3-phase feeder, with that feeder's attributes."""
    cols = [c for c in ("feeder_id", "substation", "voltage_kv", "hc_mw", "hc_mw_alt", "updated", "CIP_FEE", "Ongoing_ASO", "Potential_ASO") if c in feeders]
    if feeders.empty:
        return pd.DataFrame(index=parcels.index, columns=["dist_3ph_m", *cols])
    j = gpd.sjoin_nearest(parcels[["geometry"]], feeders[[*cols, "geometry"]], how="left", max_distance=max_m, distance_col="dist_3ph_m")
    j = j[~j.index.duplicated(keep="first")]
    return j[["dist_3ph_m", *cols]]


def nearest_substation(parcels: gpd.GeoDataFrame, subs: gpd.GeoDataFrame, name_field: str | None) -> pd.DataFrame:
    if subs.empty:
        return pd.DataFrame(index=parcels.index, columns=["dist_sub_m", "substation_nearest"])
    s = subs[["geometry"]].copy()
    s["substation_nearest"] = subs[name_field] if name_field and name_field in subs else None
    j = gpd.sjoin_nearest(parcels[["geometry"]], s, how="left", max_distance=20000, distance_col="dist_sub_m")
    j = j[~j.index.duplicated(keep="first")]
    return j[["dist_sub_m", "substation_nearest"]]
