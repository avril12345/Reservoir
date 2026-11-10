"""A synthetic two-town region with known answers, written exactly as `reservoir fetch` would leave it.

Every parcel is built to exercise one rule (setback, wetland, flood, slope, forest, landfill share, 21E point,
assemblage, road split, excluded use, open space, moratorium, zero headroom ...), so the pipeline's output for
each one can be checked against a value worked out by hand. Nothing is random, so runs are repeatable.

The world is laid out in local metres and shifted to a CRS-appropriate origin, and parcel columns are named
through the state's own config, so the same world can be run as MA, NY or any new state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.affinity import translate
from shapely.geometry import LineString, Point, box

ACRE = 4046.8564224
ORIGINS = {26986: (200_000, 880_000), 26918: (600_000, 4_600_000), 32610: (500_000, 5_000_000)}
EXTENT = (-200, -200, 3200, 2200)      # local metres, raster extent (towns + margin)
TOWN_A, TOWN_B = "TESTTOWN", "OTHERTOWN"

# Use codes by role, per state's assessor scheme. Roles are what the code should map them to.
USE_CODES = {
    "MA": {"industrial_vacant": "440", "industrial": "400", "municipal": "930", "residential": "1010", "vacant_other": "1320",
           "cemetery": "440"},     # MA: the CEMETERY keyword in USE_DESC is what excludes it
    "NY": {"industrial_vacant": "340", "industrial": "710", "municipal": "963", "residential": "210", "vacant_other": "311",
           "cemetery": "695"},     # NY ORPS 695 = cemetery; NY has no text use description
}


@dataclass(frozen=True)
class P:
    id: str
    box: tuple
    use: str                # role key in USE_CODES
    owner: str
    addr: str
    town: str = TOWN_A
    desc: str = ""


ACME, ACME_ADDR = "ACME INDUSTRIAL LLC", "1 MILL ST"
PARCELS = [
    P("IND", (0, 0, 400, 400), "industrial_vacant", ACME, ACME_ADDR),                       # clean, by-right town: the best site
    P("LANDFILL", (500, 0, 900, 400), "municipal", "TOWN OF TESTTOWN", "1 TOWN HALL RD"),      # 56 % landfill polygon, EJ block group
    P("BROWNFIELD", (1000, 0, 1300, 300), "industrial", "OLD MILL REALTY TRUST", "9 RIVER RD"),  # 21E point, energy community, a building
    P("RESID", (1400, 0, 1800, 400), "residential", "J DOE", "4 OAK ST"),                    # residential: never passes
    P("WET", (0, 600, 300, 900), "industrial_vacant", "WETCO INC", "2 BOG RD"),              # all wetland: no buildable area
    P("FLOOD", (400, 600, 800, 1000), "industrial_vacant", "FLOODCO INC", "3 RIVER RD"),     # west half in the SFHA
    P("STEEP", (900, 600, 1200, 900), "industrial_vacant", "HILLCO INC", "4 HILL RD"),       # 30 % slope throughout
    P("FOREST", (1300, 600, 1700, 1000), "industrial_vacant", "TIMBERCO INC", "5 WOOD RD"),  # all NLCD forest
    P("DISTURBED", (1800, 600, 1980, 900), "vacant_other", "SANDCO INC", "6 PIT RD"),        # NLCD barren on vacant land
    P("ASM_A", (0, 1200, 120, 1330), "industrial_vacant", "SMITH FAMILY TRUST", "5 ELM ST"),  # too small alone,
    P("ASM_B", (120, 1200, 240, 1330), "industrial_vacant", "SMITH FAMILY TRUST", "5 ELM ST"),  # passes once joined
    P("ROAD_A", (400, 1200, 520, 1330), "industrial_vacant", "JONES LLC", "7 MAPLE ST"),     # same owner, 10 m apart,
    P("ROAD_B", (530, 1200, 650, 1330), "industrial_vacant", "JONES LLC", "7 MAPLE ST"),     # but a built road between
    P("CEMETERY", (800, 1200, 1100, 1500), "cemetery", "ST MARY PARISH", "8 CHURCH ST", desc="CEMETERY"),
    P("OPENSPACE", (1200, 1200, 1500, 1500), "industrial_vacant", "LAND TRUST INC", "9 PARK RD"),  # protected open space
    P("TINY", (1600, 1200, 1650, 1250), "industrial_vacant", "TINY LLC", "10 SMALL ST"),     # 0.6 ac: dropped before analysis
    P("MORATORIUM", (2100, 0, 2500, 400), "industrial_vacant", ACME, ACME_ADDR, town=TOWN_B),  # town with active moratorium, 0 MW feeder
]
BY_ID = {p.id: p for p in PARCELS}

# Hand-worked expectations (default weights). Buildable = parcel shrunk by the 15 m property-line setback
# minus constraints; MWac = largest piece / 5 ac per MW, capped at 5.
EXPECT = {
    "IND": dict(site_type="industrial_vacant", passes=True, buildable_acres=370 * 370 / ACRE, score=87.5),
    "LANDFILL": dict(site_type="landfill", passes=True, low_income=True),
    "BROWNFIELD": dict(site_type="brownfield", passes=True, energy_community=True),
    "RESID": dict(site_type="residential", passes=False),
    "WET": dict(passes=False, buildable_acres=0.0),
    "FLOOD": dict(passes=True, flood_share=0.5),
    "STEEP": dict(passes=False),
    "FOREST": dict(passes=True, c_land_cover=0.0),
    "DISTURBED": dict(site_type="disturbed_land"),
    "CEMETERY": dict(site_type="excluded_use", passes=False),
    "OPENSPACE": dict(passes=False, open_space_share=1.0),
    "MORATORIUM": dict(passes=True, permitting_status="moratorium", c_grid_hosting=0.0),
}


def tracker_records(state: str) -> dict:
    """Siting-tracker records for the synthetic towns, keyed like tracker.load(). Dates are far in the future,
    so the result never depends on today's date."""
    from datetime import date

    from reservoir.bylaws import tracker
    from reservoir.bylaws.schema import Evidence, Moratorium, TownRecord

    recs = [
        TownRecord(state=state, municipality=TOWN_A, solar_permission="by_right", bess_permission="by_right", verified_by="test"),
        TownRecord(state=state, municipality=TOWN_B, solar_permission="by_right", bess_permission="by_right", verified_by="test",
                   moratoria=[Moratorium(technology="solar", local_law="LL 1-2099", status="active", adopted=date(2099, 1, 1),
                                         effective=None, expires=date(2099, 12, 31), duration_days=None, scope="all solar",
                                         evidence=Evidence(quote="q", section="s"))]),
    ]
    return {tracker.key(r.state, r.municipality): r for r in recs}


def _gdf(rows, geoms, crs, town=TOWN_A, shift=(0, 0)):
    g = gpd.GeoDataFrame(rows, geometry=[translate(x, *shift) for x in geoms], crs=f"EPSG:{crs}")
    if "_town" not in g:
        g["_town"] = town
    return g


def _write_rasters(d: Path, crs: int, shift):
    """10 m DEM (flat except a 30 % ramp under STEEP) and 30 m NLCD (crops, with forest and barren patches)."""
    x0, y0, x1, y1 = EXTENT
    ox, oy = shift

    def grid(res):
        xs = np.arange(x0 + res / 2, x1, res)
        ys = np.arange(y1 - res / 2, y0, -res)
        return np.meshgrid(xs, ys)

    X, Y = grid(10)
    ramp = (X >= 880) & (X <= 1220) & (Y >= 580) & (Y <= 920)
    z = np.where(ramp, 100 + 0.30 * (X - 880), 100.0)
    dem = d / "dem.tif"
    with rasterio.open(dem, "w", driver="GTiff", height=z.shape[0], width=z.shape[1], count=1, dtype="float32",
                       crs=f"EPSG:{crs}", transform=from_origin(x0 + ox, y1 + oy, 10, 10), nodata=-9999) as dst:
        dst.write(z.astype("float32"), 1)

    X, Y = grid(30)
    lc = np.full(X.shape, 82, dtype="uint8")
    lc[(X >= 1290) & (X <= 1710) & (Y >= 590) & (Y <= 1010)] = 41      # FOREST
    lc[(X >= 1790) & (X <= 1990) & (Y >= 590) & (Y <= 910)] = 31       # DISTURBED
    nlcd = d / "nlcd.tif"
    with rasterio.open(nlcd, "w", driver="GTiff", height=lc.shape[0], width=lc.shape[1], count=1, dtype="uint8",
                       crs=f"EPSG:{crs}", transform=from_origin(x0 + ox, y1 + oy, 30, 30)) as dst:
        dst.write(lc, 1)
    return dem, nlcd


def build_world(region, with_roads: bool = True) -> dict:
    """Write inputs.gpkg + rasters.json into region.out_dir. Returns layer counts (like fetch_summary.json)."""
    crs = region.crs
    shift = ORIGINS.get(crs, (0, 0))
    d = region.out_dir
    pf = region.parcel_fields
    codes = USE_CODES.get(region.state, USE_CODES["MA"])
    tid = region.sources["parcels"].get("town_id_field") or "TOWN_ID"

    rows = []
    for p in PARCELS:
        r = {"_town": p.town, tid: 1 if p.town == TOWN_A else 2}
        vals = {"parcel_id": p.id, "site_address": f"{p.id} SITE RD", "use_code": codes[p.use], "use_desc": p.desc,
                "map_par": f"M-{p.id}", "owner_name": p.owner, "owner_care_of": "", "owner_address": p.addr,
                "owner_city": "SOMEWHERE", "owner_state": region.state, "owner_zip": "01234"}
        for std, src in pf.items():
            if src:
                # use_desc and use_code can share a source column (NY): keep the code.
                r.setdefault(src, vals[std])
        rows.append(r)
    layers = {
        "towns": _gdf([{"town": TOWN_A, "town_id": 1}, {"town": TOWN_B, "town_id": 2}],
                      [box(-100, -100, 2050, 2100), box(2050, -100, 3000, 2100)], crs, shift=shift),
        "parcels": _gdf(rows, [box(*p.box) for p in PARCELS], crs, shift=shift),
        "wetlands": _gdf([{"WETLAND_TYPE": "PFO"}], [box(-10, 590, 310, 910)], crs, shift=shift),
        "flood": _gdf([{"FLD_ZONE": "AE"}], [box(380, 580, 600, 1020)], crs, shift=shift),
        "open_space": _gdf([{"LEV_PROT": "P"}], [box(1190, 1190, 1510, 1510)], crs, shift=shift),
        "buildings": _gdf([{"STRUCT_ID": 1}], [box(1100, 100, 1120, 120)], crs, shift=shift),
        "landfills_poly": _gdf([{"SITE_NAME": "TESTTOWN LANDFILL", "STATUS": "Closed", "CAP_STATUS": "Capped"}],
                               [box(550, 50, 850, 350)], crs, shift=shift),
        "brownfields_21e": _gdf([{"RTN": "2-0001234", "NAME": "OLD MILL", "STATUS": "TIER II"}], [Point(1150, 150)], crs, shift=shift),
        "low_income": _gdf([{"GEOID": "250"}], [box(450, -50, 950, 450)], crs, shift=shift),
        "energy_community": _gdf([{"id": 1}], [box(950, -50, 1350, 350)], crs, shift=shift),
        "substations": _gdf([{"NAME": "TEST SUB"}], [Point(700, 500)], crs, shift=shift),
    }
    # Feeder drawn as pixel points (what the National Grid map-export path produces): F1 has 10 MW, F2 none.
    xs = np.arange(0, 2600, 5.0)
    layers["feeders"] = _gdf(
        [{"feeder_id": "F1" if x < 1500 else "F2", "substation": "TEST SUB", "voltage_kv": 13.8, "hc_mw": 10.0 if x < 1500 else 0.0,
          "hc_mw_alt": 10.0 if x < 1500 else 0.0, "updated": "2026-01-01", "_town": TOWN_A if x < 2050 else TOWN_B} for x in xs],
        [Point(x, 450) for x in xs], crs, shift=shift)
    if with_roads:
        layers["roads"] = _gdf([{"STREETNAME": "GAP RD"}], [LineString([(525, 1150), (525, 1400)])], crs, shift=shift)

    gpkg = d / "inputs.gpkg"
    gpkg.unlink(missing_ok=True)
    for name, g in layers.items():
        g.to_file(gpkg, layer=name, driver="GPKG")
    dem, nlcd = _write_rasters(d, crs, shift)
    (d / "rasters.json").write_text(json.dumps({TOWN_A: {"dem": str(dem), "nlcd": str(nlcd)}}, indent=2))
    counts = {k: len(v) for k, v in layers.items()}
    (d / "fetch_summary.json").write_text(json.dumps(counts, indent=2))
    return counts


# ------------------------------------------------------------------------------------------ running it
class IsolatedRegion:
    """Marker base; see isolate()."""


def isolate(region, out_dir: Path):
    """A copy of `region` whose out_dir is `out_dir`, so tests never read or write the real outputs/ folder."""
    import copy

    from reservoir.config import Region

    cls = type("IsolatedRegion", (Region, IsolatedRegion), {"out_dir": property(lambda self: _mk(self._out))})
    r = copy.deepcopy(region)
    r.__class__ = cls
    r._out = Path(out_dir)
    r.id = f"synthetic_{region.id}"
    return r


def _mk(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_pipeline(region, out_dir: Path, with_roads: bool = True, write_outputs: bool = True, top: int = 50, cards: int = 5,
                 records: dict | None = None) -> dict:
    """fetch (synthetic) -> analyse -> score -> shortlist / owner list / cards, fully offline."""
    import pytest

    from reservoir import analysis, outputs, scoring
    from reservoir.bylaws import tracker

    r = isolate(region, out_dir)
    counts = build_world(r, with_roads=with_roads)
    with pytest.MonkeyPatch.context() as mp:
        # analyse reads town setbacks from the tracker too, so both steps see the synthetic records only.
        recs = tracker_records(r.state) if records is None else records
        mp.setattr(tracker, "load", lambda path=None: recs)
        sites, lots = analysis.analyse(r, progress=lambda m: None)
        scored = scoring.score(r, sites)
    res = {"region": r, "out_dir": r.out_dir, "counts": counts, "sites": sites, "lots": lots, "scored": scored}
    if write_outputs:
        short = outputs.write_shortlist(r, scored, lots, top)
        res.update(short=short, owners=outputs.write_owner_list(r, short), cards=outputs.write_cards(r, short, cards))
    return res


def by_parcel(res: dict):
    """Scored sites indexed by the representative lot's parcel id (an assemblage is keyed by its largest lot)."""
    pid = res["region"].parcel_fields["parcel_id"]
    return res["scored"].set_index(res["scored"][pid].astype(str))
