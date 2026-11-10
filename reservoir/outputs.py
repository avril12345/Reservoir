"""Shortlist, owner outreach list, and one-page site cards."""
from __future__ import annotations

import base64
import io
import json
import re
from datetime import date

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from jinja2 import Template

from .analysis import owner_key, read_inputs
from .config import Region
from .fetch import ACRE_M2

SHORTLIST_COLS = [
    "rank", "score", "parcel_id", "assemblage_id", "n_parcels", "lots_summary", "member_ids", "gap_joined",
    "_town", "site_address", "site_type", "use_type", "use_code", "use_desc",
    "parcel_acres", "buildable_acres", "largest_contig_acres", "est_mwac", "setback_ft_applied", "setback_source",
    "feeder_id", "substation", "voltage_kv", "hc_mw", "hc_mw_alt", "updated", "dist_3ph_m", "dist_sub_m", "substation_nearest",
    "permitting_status", "wetland_share", "flood_share", "open_space_share", "steep_share", "forest_share_buildable",
    "brownfield_rtns", "landfill_label", "energy_community", "low_income",
    "c_site_type", "c_grid_hosting", "c_grid_distance", "c_size", "c_permitting", "c_incentives", "c_land_cover",
    "owner_name", "owner_care_of", "owner_address", "owner_city", "owner_state", "owner_zip", "site_flags",
]


def standard_fields(region: Region, df: pd.DataFrame) -> pd.DataFrame:
    """Copy the state's parcel/owner columns (per config) into standard names; fields a source lacks are blank."""
    df = df.copy()
    for std, src in region.parcel_fields.items():
        df[std] = df[src].fillna("").astype(str).str.strip().replace({"None": "", "nan": ""}) if src and src in df else ""
    return df


def redact_private_owners(region: Region, *frames: pd.DataFrame) -> list[pd.DataFrame]:
    """For demos and screen recordings: replace individual and trust owners' names and mailing addresses with
    stable placeholders ("Private owner 7"). Businesses and public owners stay visible. The same owner gets the
    same placeholder in every frame, so owner grouping and assemblages still line up."""
    pf = region.parcel_fields
    name_c, addr_c = pf["owner_name"], pf["owner_address"]
    keys = sorted({owner_key(n, a) or str(n) for f in frames if name_c in f
                   for n, a in zip(f[name_c], f[addr_c] if addr_c in f else [""] * len(f))
                   if owner_type(n) in ("individual", "trust")})
    label = {k: f"Private owner {i + 1}" for i, k in enumerate(keys)}
    out = []
    for f in frames:
        f = f.copy()
        if name_c in f:
            addr = f[addr_c] if addr_c in f else pd.Series("", index=f.index)
            k = [owner_key(n, a) or str(n) for n, a in zip(f[name_c], addr)]
            hit = pd.Series([kk in label for kk in k], index=f.index)
            f.loc[hit, name_c] = [label[kk] for kk, h in zip(k, hit) if h]
            for c in ("care_of", "address", "city", "zip"):
                src = pf[f"owner_{c}"]
                if src and src in f:
                    f.loc[hit, src] = "(redacted)" if c == "address" else ""
            if addr_c in f:  # keep a distinct address so owner_key still separates placeholders
                f.loc[hit, addr_c] = [f"(redacted {label[kk].split()[-1]})" for kk, h in zip(k, hit) if h]
        out.append(f)
    return out


LOT_COLS = ["rank", "score", "passes_filters", "assemblage_id", "parcel_id", "_town", "site_address", "use_code", "use_desc",
            "use_type", "parcel_acres", "eligible", "owner_name", "owner_care_of", "owner_address", "owner_city", "owner_state", "owner_zip"]


def site_lots(region: Region, sites: pd.DataFrame, lots: pd.DataFrame) -> pd.DataFrame:
    """Tax lots (what you buy) behind each site (what you originate), carrying the site's rank and score."""
    keep = sites[["assemblage_id", *[c for c in ("rank", "score", "passes_filters") if c in sites]]]
    df = standard_fields(region, lots[lots.assemblage_id.isin(keep.assemblage_id)]).merge(keep, on="assemblage_id")
    df = df.sort_values(["rank", "assemblage_id", "parcel_acres"], ascending=[True, True, False])
    return df[[c for c in LOT_COLS if c in df] + (["geometry"] if "geometry" in df else [])]


def write_shortlist(region: Region, scored: gpd.GeoDataFrame, lots: gpd.GeoDataFrame, top_n: int = 50) -> gpd.GeoDataFrame:
    d = region.out_dir
    s = standard_fields(region, scored[scored.passes_filters].head(top_n))
    site_lots(region, s, lots).drop(columns="geometry").round(3).to_csv(d / "shortlist_lots.csv", index=False)
    cols = [c for c in SHORTLIST_COLS if c in s]
    flat = s[cols].copy()
    flat["site_flags"] = flat["site_flags"].apply(lambda f: " | ".join(f))
    flat.round(3).to_csv(d / "shortlist.csv", index=False)
    geo = s[[*cols, "geometry"]].copy()
    geo["site_flags"] = geo["site_flags"].apply(lambda f: " | ".join(f))
    geo.to_crs(4326).to_file(d / "shortlist.geojson", driver="GeoJSON")
    # Full scored tables for QGIS / auditing: every site (pass + fail), and every tax lot tagged with its site.
    gpkg = d / "scored_parcels.gpkg"
    gpkg.unlink(missing_ok=True)
    allp = scored.drop(columns=["buildable_geom"]).copy()
    allp["site_flags"] = allp["site_flags"].apply(lambda f: " | ".join(f))
    allp.to_file(gpkg, layer="sites", driver="GPKG")
    al = lots.merge(scored[["assemblage_id", "rank", "score", "passes_filters"]], on="assemblage_id", how="left")
    gpd.GeoDataFrame(al, geometry="geometry", crs=lots.crs).to_file(gpkg, layer="lots", driver="GPKG")
    bg = gpd.GeoDataFrame(s[["parcel_id", "rank"]], geometry=s["buildable_geom"], crs=scored.crs)
    bg.to_crs(4326).to_file(d / "shortlist_buildable.geojson", driver="GeoJSON")
    return s


_GOV = re.compile(r"\b(TOWN OF|CITY OF|COMMONWEALTH|COMMONWLTH|INHABITANTS|CONSERVATION COMM|MASS(ACHUSETTS)? (DEPT|DEPARTMENT)|STATE OF|COUNTY OF|HOUSING AUTH|WATER DIST|SCHOOL)\b")
_UTIL = re.compile(r"\b(NEW ENGLAND POWER|MASSACHUSETTS ELECTRIC|NATIONAL GRID|NSTAR|EVERSOURCE|UNITIL|TENNESSEE GAS|ALGONQUIN|NIAGARA MOHAWK)\b")
_CORP = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|CO|COMPANY|LP|LLP|LTD|REALTY|PROPERTIES|ASSOC|PARTNERS|HOLDINGS|DEVELOPMENT)\b")


def owner_type(name: str) -> str:
    n = name.upper() if isinstance(name, str) else ""
    if _GOV.search(n):
        return "public"
    if _UTIL.search(n):
        return "utility"
    if re.search(r"\b(TRUST|TRUSTEE|TR|TRS)\b", n):
        return "trust"
    if _CORP.search(n):
        return "business"
    return "individual"


def owner_list(region: Region, parcels: pd.DataFrame) -> pd.DataFrame:
    """Group sites (already passed through standard_fields) by owner key, the same key that builds assemblages."""
    df = parcels.copy()
    # Sites with no owner key (unpublished owner or address) are not grouped with each other.
    df["owner_key"] = [owner_key(n, a) or f"site:{i}" for i, n, a in zip(df.index, df.owner_name, df.owner_address)]
    if "member_ids" not in df:
        df["member_ids"], df["n_parcels"] = df["parcel_id"], 1
    g = df.groupby("owner_key").agg(
        owner_name=("owner_name", "first"), care_of=("owner_care_of", "first"), mail_address=("owner_address", "first"),
        mail_city=("owner_city", "first"), mail_state=("owner_state", "first"), mail_zip=("owner_zip", "first"),
        sites=("parcel_id", "count"), parcels=("n_parcels", "sum"), parcel_ids=("member_ids", lambda s: "; ".join(map(str, s))),
        towns=("_town", lambda s: ", ".join(sorted(set(s)))), site_addresses=("site_address", lambda s: "; ".join(map(str, s))),
        total_acres=("parcel_acres", "sum"), buildable_acres=("buildable_acres", "sum"), est_mwac=("est_mwac", "sum"),
        best_score=("score", "max"), best_rank=("rank", "min"), site_types=("site_type", lambda s: ", ".join(sorted(set(s)))),
    ).reset_index(drop=True)
    g["owner_type"] = g.owner_name.map(owner_type)
    g["outreach_note"] = g.apply(lambda r: "Absentee owner (mailing address out of state)" if r.mail_state and r.mail_state.upper() not in (region.state, "") else "", axis=1)
    return g.sort_values(["best_rank"]).round(2)


def write_owner_list(region: Region, short: gpd.GeoDataFrame) -> pd.DataFrame:
    g = owner_list(region, short)
    g.to_csv(region.out_dir / "owner_list.csv", index=False)
    return g


# ------------------------------------------------------------------------------------------ site cards
def _map_png(p, L, crs, members=None) -> str:
    g = p.geometry
    view = g.buffer(max(250, (g.area ** 0.5) * 0.6))
    # Pull the nearest feeder into frame (up to 2.5 km) so the interconnection path is visible.
    near = None
    if "feeders" in L and len(L["feeders"]) and pd.notna(p.get("dist_3ph_m")) and 0 < p.get("dist_3ph_m") <= 2500:
        idx = L["feeders"].sindex.nearest(g, return_all=False)[1][0]
        near = L["feeders"].geometry.iloc[idx]
        view = view.union(near.buffer(150)).envelope.buffer(50)
    xmin, ymin, xmax, ymax = view.bounds
    fig, ax = plt.subplots(figsize=(6.4, 5.2), dpi=130)
    clip = lambda k: L[k].iloc[L[k].sindex.query(view, predicate="intersects")] if k in L and len(L[k]) else None
    for k, kw in [("wetlands", dict(color="#7fb3d5", alpha=0.55, lw=0)), ("flood", dict(color="#aed6f1", alpha=0.5, hatch="///", lw=0)),
                  ("open_space", dict(color="#a9dfbf", alpha=0.6, lw=0)), ("landfills_poly", dict(color="#d7bde2", alpha=0.6, lw=0))]:
        c = clip(k)
        if c is not None and len(c):
            c.clip(view).plot(ax=ax, **kw)
    if p.buildable_geom is not None and not p.buildable_geom.is_empty:
        gpd.GeoSeries([p.buildable_geom], crs=crs).plot(ax=ax, color="#f5b041", alpha=0.75, lw=0)
    gpd.GeoSeries([g], crs=crs).boundary.plot(ax=ax, color="#1b2631", lw=1.6)
    if members is not None and len(members) > 1:   # tax-lot lines inside an assemblage (title still needs them)
        members.boundary.plot(ax=ax, color="#1b2631", lw=0.6, ls=":")
    f = clip("feeders")
    if f is not None and len(f):
        f = f.clip(view)
        hc = f["hc_mw"].fillna(-1)
        f.plot(ax=ax, markersize=0.6, c=["#c0392b" if v <= 0 else "#f39c12" if v < 1 else "#27ae60" for v in hc])
    if near is not None:
        from shapely.ops import nearest_points

        a, b = nearest_points(g, near)
        ax.plot([a.x, b.x], [a.y, b.y], ls="--", color="#1b2631", lw=1)
        ax.annotate(f"{p.dist_3ph_m:,.0f} m to 3φ", ((a.x + b.x) / 2, (a.y + b.y) / 2), fontsize=7, ha="center",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.8))
    sub = clip("substations")
    if sub is not None and len(sub):
        sub.plot(ax=ax, marker="s", color="black", markersize=40)
    for k, mk in [("brownfields_21e", "^"), ("brownfields_aul", "v"), ("landfills_pts", "X")]:
        c = clip(k)
        if c is not None and len(c):
            c.plot(ax=ax, marker=mk, color="#6c3483", markersize=45)
    ax.set_xlim(xmin, xmax); ax.set_ylim(ymin, ymax); ax.set_axis_off()
    sc = 200 if (xmax - xmin) < 2000 else 500
    ax.plot([xmin + 40, xmin + 40 + sc], [ymin + 40, ymin + 40], color="k", lw=3)
    ax.text(xmin + 40, ymin + 70, f"{sc} m", fontsize=7)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


CARD = Template(open(__file__.replace("outputs.py", "templates/site_card.html.j2"), encoding="utf-8").read())


def _member_lots(region: Region, p, L) -> gpd.GeoSeries | None:
    pid = region.parcel_fields["parcel_id"]
    if p.get("n_parcels", 1) <= 1 or "parcels" not in L or pid not in L["parcels"]:
        return None
    ids = str(p.member_ids).split("; ")
    return L["parcels"].loc[L["parcels"][pid].astype(str).isin(ids)].geometry


def card_context(region: Region, p, L) -> dict:
    fmt = lambda v, f="{:.1f}", d="—": d if v is None or (isinstance(v, float) and pd.isna(v)) else f.format(v)
    return {
        "region": region.name, "generated": date.today().isoformat(), "p": p,
        "map_png": _map_png(p, L, region.crs, _member_lots(region, p, L)), "fmt": fmt,
        "flags": p["site_flags"],
        "components": [("Site type", p.c_site_type), ("Feeder headroom", p.c_grid_hosting), ("3-phase distance", p.c_grid_distance),
                       ("Size", p.c_size), ("Permitting", p.c_permitting), ("Incentives", p.c_incentives), ("Land cover", p.c_land_cover)],
        "portal": (region.hosting() or {}).get("portal", ""),
    }


def write_cards(region: Region, short: gpd.GeoDataFrame, n: int = 25) -> list[str]:
    L = read_inputs(region)
    d = region.out_dir / "cards"
    d.mkdir(exist_ok=True)
    for old in d.glob("card_*.html"):
        old.unlink()
    paths, bodies = [], []
    for _, p in short.head(n).iterrows():
        ctx = card_context(region, p, L)
        fn = d / f"card_{int(p['rank']):03d}_{re.sub(r'[^A-Za-z0-9]+', '_', str(p['parcel_id']))}.html"
        fn.write_text(CARD.render(**ctx, standalone=True), encoding="utf-8")
        paths.append(str(fn))
        bodies.append(CARD.render(**ctx, standalone=False))
    # One printable file: Ctrl+P -> "Save as PDF" gives one page per site.
    allhtml = CARD.render(region=region.name, multi=bodies, standalone=True, p=None)
    (d / "all_cards.html").write_text(allhtml, encoding="utf-8")
    return paths
