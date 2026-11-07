"""Raster inputs: USGS 3DEP elevation -> slope, and NLCD land cover. Fetched per town bbox and cached."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np
import rasterio
import requests
from pyproj import Transformer
from rasterio.features import shapes
from shapely.geometry import shape

from .arcgis import CACHE_DIR, USER_AGENT

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": USER_AGENT}


def _cache_path(*parts) -> Path:
    key = hashlib.sha1(repr(parts).encode()).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.tif"


def fetch_dem(image_server: str, bbox: tuple[float, float, float, float], crs: int, res_m: float = 10) -> Path:
    """3DEP DEM for bbox (in projected `crs`, meters) via ImageServer/exportImage. Returns GeoTIFF path."""
    path = _cache_path("dem", image_server, bbox, crs, res_m)
    if path.exists():
        return path
    xmin, ymin, xmax, ymax = bbox
    w, h = int((xmax - xmin) / res_m), int((ymax - ymin) / res_m)
    if max(w, h) > 4000:  # ImageServer size cap; coarsen rather than tile for a screening tool
        scale = max(w, h) / 4000
        w, h = int(w / scale), int(h / scale)
    params = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": crs, "imageSR": crs,
        "size": f"{w},{h}", "format": "tiff", "pixelType": "F32",
        "interpolation": "RSP_BilinearInterpolation", "f": "image",
    }
    r = requests.get(f"{image_server}/exportImage", params=params, headers=HEADERS, timeout=300)
    r.raise_for_status()
    if not r.content.startswith((b"II*", b"MM\x00*")):
        raise RuntimeError(f"3DEP did not return a TIFF: {r.text[:300]}")
    path.write_bytes(r.content)
    return path


def fetch_nlcd(wcs: str, coverage: str, bbox: tuple[float, float, float, float], crs: int) -> Path:
    """NLCD land cover clipped to bbox (given in `crs`), via MRLC WCS 2.0.1 in native EPSG:5070."""
    path = _cache_path("nlcd", wcs, coverage, bbox, crs)
    if path.exists():
        return path
    t = Transformer.from_crs(crs, 5070, always_xy=True)
    xs, ys = t.transform([bbox[0], bbox[2], bbox[0], bbox[2]], [bbox[1], bbox[1], bbox[3], bbox[3]])
    pad = 60
    params = [
        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
        ("coverageid", coverage), ("format", "image/geotiff"),
        ("subset", f"X({min(xs) - pad},{max(xs) + pad})"), ("subset", f"Y({min(ys) - pad},{max(ys) + pad})"),
    ]
    r = requests.get(wcs, params=params, headers=HEADERS, timeout=300)
    r.raise_for_status()
    if not r.content.startswith((b"II*", b"MM\x00*")):
        raise RuntimeError(f"NLCD WCS did not return a TIFF: {r.text[:300]}")
    path.write_bytes(r.content)
    return path


def slope_pct(dem_path: Path) -> tuple[np.ndarray, rasterio.Affine, object]:
    with rasterio.open(dem_path) as src:
        z = src.read(1).astype("float64")
        nodata = src.nodata
        transform, crs = src.transform, src.crs
    if nodata is not None:
        z[z == nodata] = np.nan
    z[z < -1000] = np.nan
    dzdy, dzdx = np.gradient(z, abs(transform.e), transform.a)
    return np.hypot(dzdx, dzdy) * 100.0, transform, crs


def mask_to_polygons(mask: np.ndarray, transform, crs, min_area_m2: float = 400.0):
    """Vectorise a boolean raster mask to a GeoSeries of polygons (in the raster CRS)."""
    import geopandas as gpd

    polys = [shape(g) for g, v in shapes(mask.astype("uint8"), mask=mask, transform=transform) if v == 1]
    gs = gpd.GeoSeries(polys, crs=crs)
    return gs[gs.area >= min_area_m2] if len(gs) else gs


def steep_polygons(dem_path: Path, max_pct: float):
    s, transform, crs = slope_pct(dem_path)
    return mask_to_polygons(np.nan_to_num(s, nan=0) > max_pct, transform, crs)


def landcover_polygons(nlcd_path: Path, classes: list[int]):
    with rasterio.open(nlcd_path) as src:
        a = src.read(1)
        return mask_to_polygons(np.isin(a, classes), src.transform, src.crs, min_area_m2=900)
