"""Reservoir — DG site screener + local siting & moratorium tracker.

    uv run streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from datetime import date
from pathlib import Path

import folium
import geopandas as gpd
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # cache paths are relative to repo root

from reservoir import outputs, scoring  # noqa: E402
from reservoir.analysis import read_inputs  # noqa: E402
from reservoir.bylaws import accuracy, tracker  # noqa: E402
from reservoir.bylaws.schema import TownRecord  # noqa: E402
from reservoir.config import IS_DEMO, OUTPUTS, list_regions, load_region  # noqa: E402

st.set_page_config(page_title="Reservoir Site Screener", page_icon="☀️", layout="wide")
st.markdown("""<style>
.block-container{padding-top:1.4rem}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.p-ok{background:#d5f5e3;color:#145a32}.p-warn{background:#fdebd0;color:#7e5109}.p-bad{background:#fadbd8;color:#78281f}.p-unk{background:#e5e8e8;color:#424949}
</style>""", unsafe_allow_html=True)

COMPONENTS = ["site_type", "grid_hosting", "grid_distance", "size", "permitting", "incentives", "land_cover"]


# ------------------------------------------------------------------------------------------- data
@st.cache_resource(show_spinner="Loading region inputs…")
def region_inputs(region_id: str, mtime: float):
    r = load_region(region_id)
    return r, read_inputs(r)


@st.cache_data(show_spinner="Scoring parcels…")
def scored_parcels(region_id: str, mtime: float, tracker_mtime: float) -> gpd.GeoDataFrame:
    r = load_region(region_id)
    df = pickle.loads((r.out_dir / "metrics.pkl").read_bytes())
    return scoring.score(r, df)


@st.cache_data(show_spinner=False)
def region_lots(region_id: str, mtime: float) -> gpd.GeoDataFrame:
    return pickle.loads((load_region(region_id).out_dir / "lots.pkl").read_bytes())


@st.cache_data(show_spinner=False)
def feeder_geojson(region_id: str, mtime: float, grid_m: float = 30.0) -> dict:
    """Feeder pixels snapped to a grid_m grid, one MultiPoint feature per feeder. Same look as plotting every
    pixel, at ~1/30 of the page weight (one marker per pixel made each rerun take ~15 s)."""
    _, L = region_inputs(region_id, mtime)
    fl = L.get("feeders")
    if fl is None or fl.empty:
        return {"type": "FeatureCollection", "features": []}
    k = pd.DataFrame({"f": fl.feeder_id.astype(str), "hc": fl.hc_mw,
                      "x": (fl.geometry.centroid.x // grid_m + 0.5) * grid_m,
                      "y": (fl.geometry.centroid.y // grid_m + 0.5) * grid_m}).drop_duplicates(["f", "x", "y"])
    pts = gpd.GeoDataFrame(k, geometry=gpd.points_from_xy(k.x, k.y), crs=fl.crs).to_crs(4326)
    feats = []
    for fid, d in pts.groupby("f"):
        hc = d.hc.iloc[0]
        col = "#c0392b" if pd.isna(hc) or hc <= 0 else "#f39c12" if hc < 1 else "#27ae60"
        feats.append({"type": "Feature", "properties": {"feeder_id": fid, "hc_mw": None if pd.isna(hc) else round(float(hc), 2), "color": col},
                      "geometry": {"type": "MultiPoint", "coordinates": [[round(x, 5), round(y, 5)] for x, y in zip(d.geometry.x, d.geometry.y)]}})
    return {"type": "FeatureCollection", "features": feats}


@st.cache_data(show_spinner=False, max_entries=64)
def card_html(region_id: str, key: tuple, _p) -> str:
    """Rendered site card. Every tab runs on each rerun, so without this the card's map image is redrawn
    whenever any widget changes. `key` holds everything on the card that can change between reruns."""
    r = load_region(region_id)
    _, L = region_inputs(region_id, mtime(r.out_dir / "inputs.gpkg"))
    return outputs.CARD.render(**outputs.card_context(r, _p, L), standalone=True)


def available_regions() -> list[str]:
    return [rid for rid in list_regions() if (OUTPUTS / rid / "metrics.pkl").exists() and (OUTPUTS / rid / "lots.pkl").exists()]


def mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else 0.0


def pill(status: str) -> str:
    cls = {"by_right": "p-ok", "site_plan_review": "p-ok", "special_permit": "p-warn", "not_addressed": "p-unk",
           "unknown": "p-unk", "moratorium": "p-bad", "prohibited": "p-bad"}.get(status, "p-unk")
    return f'<span class="pill {cls}">{status.replace("_", " ")}</span>'


# ------------------------------------------------------------------------------------------- sidebar
st.sidebar.title("☀️ Reservoir")
st.sidebar.caption("DG solar + storage site screener with a local siting & moratorium tracker")
regions = available_regions()
if not regions:
    st.warning("No screened regions yet. Run `uv run reservoir run --region ma_ngrid_blackstone` first.")
    st.stop()
region_id = st.sidebar.selectbox("Region", regions, format_func=lambda r: load_region(r).name)
region, L = region_inputs(region_id, mtime(OUTPUTS / region_id / "inputs.gpkg"))
scored = scored_parcels(region_id, mtime(region.out_dir / "metrics.pkl"), mtime(tracker.TRACKER))
lots = region_lots(region_id, mtime(region.out_dir / "lots.pkl"))
if os.environ.get("RESERVOIR_DEMO_REDACT") == "1":  # screen recordings: mask private individuals' names/addresses
    scored, lots = outputs.redact_private_owners(region, scored, lots)
if IS_DEMO or os.environ.get("RESERVOIR_DEMO_REDACT") == "1":  # demo_data/ is already redacted at build time
    st.sidebar.info("Demo mode: individual and trust owners are masked.")
base = outputs.standard_fields(region, scored)
meta = json.loads((region.out_dir / "run_meta.json").read_text()) if (region.out_dir / "run_meta.json").exists() else {}

with st.sidebar.expander("Score weights (live re-rank)", expanded=False):
    dw = region.weights["weights"]
    w = {k: st.slider(k.replace("_", " "), 0, 40, int(dw[k]), key=f"w_{k}") for k in COMPONENTS}
    st.caption("Default weights live in `config/weights/default.yaml`.")
tot = sum(w.values()) or 1
base["score"] = (100 * sum(w[k] * base[f"c_{k}"] for k in COMPONENTS) / tot).round(1)
base["rank"] = base["score"].where(base.passes_filters).rank(ascending=False, method="first")

st.sidebar.markdown("**Filters**")
towns = sorted(base["_town"].unique())
f_towns = st.sidebar.multiselect("Towns", towns, default=towns)
types = sorted(base.loc[base.passes_filters, "site_type"].unique())
priority = [t for t in ("landfill", "brownfield", "gravel_pit", "industrial_vacant", "industrial") if t in types]
f_types = st.sidebar.multiselect("Site types", types, default=types)
f_min_mw = st.sidebar.slider("Min est. MWac", 0.0, 5.0, 0.5, 0.25)
f_hc = st.sidebar.checkbox("Only feeders with headroom > 0 MW", value=False)
f_perm = st.sidebar.checkbox("Hide towns with active moratoria", value=False)
f_n = st.sidebar.slider("Show top N", 10, 300, 75, 5)

view = base[base.passes_filters & base["_town"].isin(f_towns) & base.site_type.isin(f_types) & (base.est_mwac >= f_min_mw)]
if f_hc:
    view = view[view.hc_mw.fillna(0) > 0]
if f_perm:
    view = view[view.permitting_status != "moratorium"]
view = view.sort_values("score", ascending=False).head(f_n)

st.sidebar.divider()
st.sidebar.caption(f"Run: {meta.get('generated', '?')} · {len(lots):,} tax lots → {len(base):,} sites "
                   f"({int((base.n_parcels > 1).sum()):,} same-owner assemblages) · {int(base.passes_filters.sum()):,} pass filters")

# ------------------------------------------------------------------------------------------- header
st.title(region.name)
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Sites shown", len(view), help=f"{int(view.n_parcels.sum())} tax lots; {int((view.n_parcels > 1).sum())} sites are same-owner assemblages")
c2.metric("Est. MWac (shown)", f"{view.est_mwac.sum():.1f}")
c3.metric("Priority sites", int(view.site_type.isin(["landfill", "brownfield", "gravel_pit"]).sum()),
          help="Landfills, brownfields (21E/AUL), gravel pits")
c4.metric("On feeders with headroom", int((view.hc_mw.fillna(0) > 0).sum()))
c5.metric("Distinct owners", view["owner_name"].replace("", pd.NA).nunique())

tabs = st.tabs(["🗺️ Map", "📋 Shortlist", "🪪 Site cards", "📇 Owner outreach", "🏛️ Siting & moratorium tracker",
                "✅ Accuracy check", "📚 Method & sources"])

# ------------------------------------------------------------------------------------------- map
with tabs[0]:
    v4 = view.to_crs(4326)
    if len(v4):
        cen = v4.geometry.union_all().centroid
        m = folium.Map(location=[cen.y, cen.x], zoom_start=12, tiles="OpenStreetMap", control_scale=True)
        folium.TileLayer("Esri.WorldImagery", name="Imagery", show=False).add_to(m)
        twn = L["towns"].to_crs(4326)
        folium.GeoJson(twn[["town", "geometry"]], name="Towns", style_function=lambda f: {"color": "#566573", "weight": 1.5, "fill": False},
                       tooltip=folium.GeoJsonTooltip(["town"])).add_to(m)
        fgj = feeder_geojson(region_id, mtime(OUTPUTS / region_id / "inputs.gpkg"))
        if fgj["features"]:
            folium.GeoJson(fgj, name="3-phase feeders (color = headroom)",
                           marker=folium.CircleMarker(radius=1.5, fill=True, weight=0, fill_opacity=0.9),
                           # No tooltip: Leaflet can't bind one to a MultiPoint layer (breaks the whole map).
                           # Parcel tooltips already show the nearest feeder and its headroom.
                           style_function=lambda f: {"fillColor": f["properties"]["color"], "color": f["properties"]["color"]}).add_to(m)
        sub = L.get("substations")
        if sub is not None and len(sub):
            g = folium.FeatureGroup("Substations")
            for _, r in sub.to_crs(4326).iterrows():
                folium.Marker([r.geometry.y, r.geometry.x], icon=folium.Icon(color="black", icon="bolt", prefix="fa"),
                              tooltip=str(r.get("NAME", "substation"))).add_to(g)
            g.add_to(m)
        cmap = lambda s: "#1a5276" if s >= 75 else "#2e86c1" if s >= 65 else "#85c1e9"
        cols = ["rank", "score", "site_address", "_town", "site_type", "n_parcels", "est_mwac", "hc_mw", "feeder_id", "owner_name", "permitting_status"]
        folium.GeoJson(v4[[*cols, "geometry"]], name="Candidate parcels",
                       style_function=lambda f: {"color": cmap(f["properties"]["score"]), "weight": 2, "fillOpacity": 0.25},
                       tooltip=folium.GeoJsonTooltip(cols, aliases=["Rank", "Score", "Address", "Town", "Site type", "Lots", "MWac", "HC MW", "Feeder", "Owner", "Permitting"])).add_to(m)
        bg = gpd.GeoDataFrame(view[["rank"]], geometry=view["buildable_geom"], crs=view.crs).to_crs(4326)
        bg = bg[~bg.geometry.is_empty]
        folium.GeoJson(bg, name="Buildable area", style_function=lambda f: {"color": "#b9770e", "weight": 0, "fillColor": "#f5b041", "fillOpacity": 0.6}).add_to(m)
        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, height=640, use_container_width=True, returned_objects=[])
        st.caption("Feeder positions are rebuilt from the utility portal's rendered map (about ±12 m); hosting capacity is net of pending DG.")
    else:
        st.info("No parcels match the filters.")

# ------------------------------------------------------------------------------------------- shortlist
with tabs[1]:
    show = ["rank", "score", "_town", "site_address", "site_type", "n_parcels", "lots_summary", "use_desc", "parcel_acres", "buildable_acres",
            "largest_contig_acres", "est_mwac", "feeder_id", "hc_mw", "dist_3ph_m", "substation", "permitting_status",
            "owner_name", *[f"c_{k}" for k in COMPONENTS]]
    t = view[show].copy()
    t["site_flags"] = view["site_flags"].apply(lambda f: " | ".join(f))
    st.dataframe(
        t, hide_index=True, use_container_width=True, height=560,
        column_config={
            "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f"),
            "_town": "Town", "site_address": "Address", "owner_name": "Owner", "use_desc": "Assessor use",
            "n_parcels": st.column_config.NumberColumn("Lots", format="%d"), "lots_summary": "Assemblage",
            "parcel_acres": st.column_config.NumberColumn("Site ac", format="%.1f"),
            "buildable_acres": st.column_config.NumberColumn("Buildable ac", format="%.1f"),
            "largest_contig_acres": st.column_config.NumberColumn("Largest piece ac", format="%.1f"),
            "est_mwac": st.column_config.NumberColumn("Est. MWac", format="%.2f"),
            "hc_mw": st.column_config.NumberColumn("Feeder HC MW", format="%.2f"),
            "dist_3ph_m": st.column_config.NumberColumn("To 3φ (m)", format="%.0f"),
            **{f"c_{k}": st.column_config.ProgressColumn(k.replace("_", " "), min_value=0, max_value=1, format="%.2f") for k in COMPONENTS},
        })
    d1, d2, d3 = st.columns(3)
    d1.download_button("⬇ Shortlist CSV", t.to_csv(index=False), f"{region.id}_shortlist.csv", "text/csv")
    g = view.drop(columns=["buildable_geom"]).copy()
    g["site_flags"] = g["site_flags"].apply(lambda f: " | ".join(f))
    d2.download_button("⬇ Shortlist GeoJSON", g.to_crs(4326).to_json(), f"{region.id}_shortlist.geojson", "application/geo+json")
    d3.caption("Every site (pass + fail) and every tax lot are layers `sites` and `lots` in `outputs/<region>/scored_parcels.gpkg` for QGIS.")
    sl = outputs.site_lots(region, view, lots)
    with st.expander(f"🧾 Tax lots behind these sites ({len(sl)} lots in {len(view)} sites): what you buy"):
        only_multi = st.checkbox("Only assemblages (2+ lots)", value=True)
        if only_multi:
            sl = sl[sl.assemblage_id.isin(view.loc[view.n_parcels > 1, "assemblage_id"])]
        lt = sl.drop(columns=["geometry"], errors="ignore")
        st.dataframe(lt, hide_index=True, use_container_width=True,
                     column_config={"parcel_acres": st.column_config.NumberColumn("Lot ac", format="%.2f"),
                                    "score": st.column_config.NumberColumn("Site score", format="%.0f"), "rank": "Site rank"})
        st.download_button("⬇ Lots CSV (title / deed research)", lt.to_csv(index=False), f"{region.id}_lots.csv", "text/csv")

# ------------------------------------------------------------------------------------------- site cards
with tabs[2]:
    if len(view):
        opts = {f"#{int(r['rank'])} · {r['site_address'] or r['parcel_id']}, {r['_town'].title()} · {r.site_type.replace('_', ' ')}{f' · {r.n_parcels} lots' if r.n_parcels > 1 else ''} · {r.est_mwac:.1f} MWac": i for i, r in view.iterrows()}
        pick = st.selectbox("Parcel", list(opts))
        p = view.loc[opts[pick]]
        html = card_html(region_id, (str(p["parcel_id"]), p["rank"], p["score"], tuple(p["site_flags"]), p["owner_name"]), p)
        components.html(html, height=1020, scrolling=True)
        c1, c2 = st.columns(2)
        c1.download_button("⬇ This card (HTML, print to PDF)", html, f"site_card_{p['parcel_id']}.html", "text/html")
        allc = region.out_dir / "cards" / "all_cards.html"
        if allc.exists():
            c2.download_button("⬇ All pre-rendered cards (default weights)", allc.read_text(encoding="utf-8"), f"{region.id}_cards.html", "text/html")
    else:
        st.info("No parcels match the filters.")

# ------------------------------------------------------------------------------------------- owners
with tabs[3]:
    grp = outputs.owner_list(region, view)
    types_sel = st.multiselect("Owner type", sorted(grp.owner_type.unique()), default=sorted(grp.owner_type.unique()))
    grp = grp[grp.owner_type.isin(types_sel)]
    grp["absentee"] = grp.mail_state.fillna("").str.upper().ne(region.state) & grp.mail_state.fillna("").ne("")
    st.dataframe(grp, hide_index=True, use_container_width=True, height=520,
                 column_config={"est_mwac": st.column_config.NumberColumn("Est. MWac", format="%.2f"),
                                "best_score": st.column_config.NumberColumn("Best score", format="%.0f")})
    st.download_button("⬇ Owner list CSV (mail-merge ready)", grp.to_csv(index=False), f"{region.id}_owners.csv", "text/csv")
    st.caption("Owner and mailing data come from public municipal assessor records. Owners with several candidate parcels are grouped. "
               "Check the current deed before contacting anyone. Assessor data can lag sales by a year.")

# ------------------------------------------------------------------------------------------- siting tracker
with tabs[4]:
    recs = tracker.load()
    today = date.today()
    rows = []
    for k, r in recs.items():
        act = r.active_moratoria(today)
        status = "moratorium" if act else r.solar_permission
        rows.append({"State": r.state, "Municipality": r.municipality, "County": r.county,
                     "Status": status, "Solar ground-mount": r.solar_permission, "BESS": r.bess_permission,
                     "Active moratoria": "; ".join(f"{m.technology.upper()} {m.local_law}" + (f" → {m.expires}" if m.expires else "") for m in act),
                     "MA consolidated permit": r.ma_consolidated_permit, "Method": r.method,
                     "Verified": f"✔ {r.verified_by}" if r.verified_by else "—", "Updated": r.updated})
    tdf = pd.DataFrame(rows).sort_values(["State", "Municipality"])
    a, b, c, d = st.columns(4)
    a.metric("Towns tracked", len(tdf))
    b.metric("With active moratoria", int((tdf.Status == "moratorium").sum()))
    c.metric("Human-verified", int(tdf.Verified.ne("—").sum()))
    d.metric("MA consolidated-permit deadline", "2026-10-01", help="225 CMR 29.00: municipalities must accept Consolidated Local Permit applications for small clean energy infrastructure by Oct 1, 2026.")
    st.dataframe(tdf, hide_index=True, use_container_width=True)

    st.subheader("Town detail")
    sel = st.selectbox("Municipality", [f"{r.state} · {r.municipality}" for r in sorted(recs.values(), key=lambda r: (r.state, r.municipality))])
    rec = recs[tracker.key(*sel.split(" · "))]
    st.markdown(f"**{rec.municipality}, {rec.state}** ({rec.county} County) · solar {pill(rec.solar_permission)} · BESS {pill(rec.bess_permission)} · method `{rec.method}`", unsafe_allow_html=True)
    if rec.notes:
        st.info(rec.notes)
    for m in rec.moratoria:
        with st.container(border=True):
            st.markdown(f"**{m.technology.upper()} moratorium**: {m.local_law} · status **{m.status}** · adopted {m.adopted or '?'} · expires {m.expires or ('?' if not m.duration_days else f'{m.duration_days} days from effective date')}")
            st.caption(m.scope)
            st.markdown(f"> {m.evidence.quote}  \n— *{m.evidence.section}*")
    if rec.quote_check:
        st.markdown("**Quote check** (was each LLM quote really in the source?): " + ", ".join(f"`{k}`: {v}" for k, v in rec.quote_check.items()))
    st.markdown("**Sources**")
    for s in rec.sources:
        st.markdown(f"- [{s.get('title')}]({s.get('url')}) · {s.get('kind', '')} · retrieved {s.get('retrieved', '')}")

    with st.expander("✍️ Human verification: confirm or correct this record"):
        with st.form("verify"):
            perms = ["by_right", "site_plan_review", "special_permit", "prohibited", "not_addressed", "unknown"]
            sp = st.selectbox("Solar ground-mount (DG scale)", perms, index=perms.index(rec.solar_permission))
            bp = st.selectbox("BESS (multi-MWh)", perms, index=perms.index(rec.bess_permission))
            ss = st.number_input("Solar setback (ft, max of front/side/rear)", value=float(rec.solar_setback_ft or 0))
            cp = st.selectbox("MA consolidated permit", ["adopted_local_bylaw", "state_default", "unknown", "n/a"],
                              index=["adopted_local_bylaw", "state_default", "unknown", "n/a"].index(rec.ma_consolidated_permit))
            who = st.text_input("Verified by (initials)")
            note = st.text_area("Verification note / source checked", value="")
            if st.form_submit_button("Save as verified") and who:
                upd = rec.model_copy(update={"solar_permission": sp, "bess_permission": bp, "solar_setback_ft": ss or None,
                                             "ma_consolidated_permit": cp, "verified_by": who, "verified_on": today,
                                             "notes": (rec.notes + " | VERIFIED: " + note).strip(" |"), "updated": today})
                tracker.upsert(upd)
                st.success("Saved. Parcel permitting scores will refresh.")
                st.cache_data.clear()

    with st.expander("🔎 Find likely sources"):
        st.caption("Discovery suggests links: town sites, code hosts (eCode360, Municode, General Code), one hop of links from "
                   "them, any URL you add, and web search if `BRAVE_SEARCH_API_KEY` is set. Wrong-town, draft, model-bylaw and "
                   "dead links are set aside. **A person picks the governing document.** Extraction then runs the usual quote "
                   "check, and human-verified records are never overwritten. Scores don't change until you extract.")
        from reservoir.bylaws import discover as dsc

        with st.form("discover"):
            c1, c2, c3 = st.columns([1, 2, 2])
            d_state = c1.selectbox("State", ["MA", "NY", "OR"], key="d_state")
            d_town = c2.text_input("Municipality", key="d_town")
            d_county = c3.text_input("County (optional)", key="d_county")
            d_seed = st.text_input("Extra URL to check (optional)", key="d_seed")
            d_go = st.form_submit_button("Search")
        if d_go and d_town:
            try:
                with st.spinner("Fetching candidate documents (cached after the first run)…"):
                    st.session_state["discover_res"] = dsc.discover(d_state, d_town, d_county, [d_seed] if d_seed else [],
                                                                    progress=lambda m: None)
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")
        res = st.session_state.get("discover_res")
        if res is not None:
            st.markdown(f"**{res.municipality}, {res.state}** · {len(res.candidates)} shown, {len(res.more)} more, "
                        f"{len(res.rejects)} set aside · web search {'on' if res.searched_web else 'off'}"
                        + (f" · {len(res.new_urls)} new since {res.previous_run}" if res.previous_run else ""))
            row = lambda c: {"rank": c.rank, "kind": c.kind, "municipality_match": c.municipality_match, "title": c.title,
                             "url": c.url, "status": c.http_status, "reason": c.reject_reason}
            if res.candidates:
                st.dataframe(pd.DataFrame([row(c) for c in res.candidates]).drop(columns="reason"), hide_index=True,
                             use_container_width=True, column_config={"url": st.column_config.LinkColumn("URL")})
                for c in res.candidates:
                    if c.extractable and st.button(f"Extract #{c.rank}: {c.title[:70] or c.url}", key=f"dx_{c.rank}_{c.url}"):
                        from reservoir.bylaws import extract as ex

                        try:
                            with st.spinner("Reading the document with Claude…"):
                                new, raw = ex.extract_to_tracker(res.state, res.municipality, c.url, res.county)
                            st.success(f"Extracted {res.municipality} from #{c.rank}. Quote check: {new.quote_check}")
                            st.cache_data.clear()
                        except Exception as e:  # surfaced to the analyst rather than swallowed
                            st.error(f"{type(e).__name__}: {e}")
            else:
                st.info("No usable candidates. Add a URL you know, or set BRAVE_SEARCH_API_KEY to search the web.")
            if res.rejects and st.toggle(f"Show {len(res.rejects)} set-aside links (wrong town, 403/404, drafts, model bylaws)", key="d_rej"):
                st.dataframe(pd.DataFrame([row(c) for c in res.rejects]).drop(columns="rank"), hide_index=True,
                             use_container_width=True, column_config={"url": st.column_config.LinkColumn("URL")})

    with st.expander("🤖 Extract a bylaw / local law with Claude"):
        st.caption("Paste a URL to a PDF/HTML bylaw or upload a file. Needs `ANTHROPIC_API_KEY` (or an `ant auth login` profile). "
                   "Every field must come with a verbatim quote, and code checks each quote against the source. "
                   "Unsupported values are downgraded to *unknown*.")
        with st.form("extract"):
            st_ = st.selectbox("State", ["MA", "NY", "OR"])
            town = st.text_input("Municipality")
            county = st.text_input("County (optional)")
            url = st.text_input("Source URL (PDF or HTML)")
            up = st.file_uploader("…or upload PDF/TXT", type=["pdf", "txt", "html"])
            go = st.form_submit_button("Extract")
        if go and town and (url or up):
            from reservoir.bylaws import extract as ex

            src = url
            if up is not None:
                tmp = ROOT / "data" / "bylaws" / "docs" / up.name
                tmp.write_bytes(up.getvalue())
                src = str(tmp)
            try:
                with st.spinner("Reading the document with Claude…"):
                    new, raw = ex.extract_to_tracker(st_, town, src, county)
                st.success(f"Extracted {town}. Quote check: {new.quote_check}")
                st.json(raw.model_dump(mode="json"))
                st.cache_data.clear()
            except Exception as e:  # surfaced to the analyst rather than swallowed
                st.error(f"{type(e).__name__}: {e}")

# ------------------------------------------------------------------------------------------- accuracy
with tabs[5]:
    rep = accuracy.report()
    st.markdown("### Human-verified accuracy check")
    st.markdown(
        "LLM extraction of bylaws fails in predictable ways. This check samples **20 towns** "
        "(stratified by state and by whether a moratorium exists). A person reads each primary source and records the "
        "truth, and the tracker's values are scored field by field.")
    k1, k2, k3 = st.columns(3)
    k1.metric("Towns hand-checked", f"{rep.get('hand_checked_towns', 0)} / {rep.get('target', 20)}")
    ov = rep.get("overall") or {}
    k2.metric("Field-level accuracy", f"{ov['accuracy']:.0%}" if ov.get("accuracy") is not None else "—",
              help="Wilson 95% interval shown in the table" if ov else None)
    k3.metric("Fields checked", ov.get("checked", 0))
    if rep.get("per_field"):
        pf = pd.DataFrame(rep["per_field"]).T.reset_index().rename(columns={"index": "field"})
        st.dataframe(pf, hide_index=True, use_container_width=True)
        st.markdown("**Errors by type**")
        st.bar_chart(pd.Series(rep.get("error_types", {}), name="count"))
        for et, ex_ in rep.get("examples", {}).items():
            st.markdown(f"*{et}*: " + "; ".join(ex_))
    else:
        st.warning(rep.get("next_step", "No ground truth yet."))
    c1, c2 = st.columns(2)
    if c1.button("Generate / refresh 20-town worksheet"):
        accuracy.write_worksheet()
    if accuracy.WORKSHEET.exists():
        c1.download_button("⬇ Verification worksheet CSV", accuracy.WORKSHEET.read_text(encoding="utf-8"), "verification_worksheet.csv", "text/csv")
    gt = c2.file_uploader("Upload completed ground_truth.csv", type=["csv"])
    if gt is not None:
        accuracy.TRUTH.write_bytes(gt.getvalue())
        c2.success("Saved. Re-open this tab to see scores.")
    st.markdown("""
#### Error modes this check is built to catch
| Error mode | Example | Guard in this tool |
|---|---|---|
| **Hallucinated value** | invents a 50 ft setback that isn't in the text | verbatim quote required; code checks it and downgrades unsupported fields to *unknown* |
| **Omission** | misses a special-permit requirement buried in a use table | "not_addressed" is allowed, so gaps show as gaps; reviewer checks use tables |
| **Pathway confusion** | reports *by right* because small rooftop systems are allowed | prompt asks for the DG-scale (1–5 MW) pathway; field-level scoring isolates it |
| **Stale law** | reports an expired moratorium as active, or misses an extension | expiry re-derived in code from dates; "past expiry, check for extension" flag |
| **Entity confusion** | mixes **Cortlandt** (Westchester) with **Cortlandville** (Cortland Co.) | both kept as separate seeds; prompt asks the model to flag a municipality mismatch |
| **Units / AC–DC** | "5 MW" without AC/DC, kWh vs kW for BESS tiers | `max_size_text` kept as written; normalised value only when unambiguous |
| **Proposed vs adopted** | treats a draft law (Cortlandville's 2026 Tier 2 ban) as in force | status enum separates *proposed*; proposed items never change scores |
""")

# ------------------------------------------------------------------------------------------- method
with tabs[6]:
    st.markdown(f"""
### Method
**Candidates.** Sites ≥ {region.weights['project']['min_parcel_acres']} ac in {', '.join(t.title() for t in towns)}. A site is one assessor lot,
or an *assemblage*: lots ≥ {region.weights.get('assemblage', {}).get('min_lot_acres', '?')} ac with the same owner name + mailing address that touch or sit within
{region.weights.get('assemblage', {}).get('max_gap_m', '?')} m of each other in the same town. Assemblages are dissolved *before* the setback and constraints, so interior lot
lines don't eat buildable area. Residential, excluded-use and conservation lots never join one. Every tax lot is kept for title work.

**Buildable area** = site − property-line setback (the town's solar setback when the siting tracker has `solar_setback_ft` for it; otherwise the default
{region.weights['setbacks_m']['property_line']} m / ~{region.weights['setbacks_m']['property_line'] / 0.3048:.0f} ft; each card shows which) − wetlands (NWI + MassDEP)
+ {region.weights['setbacks_m']['wetland_buffer']} m buffer − FEMA SFHA − protected open space (perpetual/temporary) − slope > {region.weights['setbacks_m']['slope_max_pct']}% (3DEP 10 m)
− existing buildings + {region.weights['setbacks_m'].get('building_buffer', 10)} m. Capacity ≈ largest contiguous piece ÷ {region.weights['project']['acres_per_mwac']} ac/MWac, capped at {region.weights['project']['max_mwac']} MW.
After changing a town's setback in the tracker, rerun `reservoir score --reanalyse` to recompute geometry; permitting-only edits rescore without it.

**Site type** (East Light pattern, SMART 3.0 brownfield/landfill priority). Landfill (MassDEP solid-waste polygons/points) → brownfield
(21E tier-classified or AUL site on the parcel) → gravel pit (DOR 410–412) → disturbed land (≥ 30% NLCD barren on a vacant parcel) → assessor use code. Residential, conservation (Article 97), and
excluded uses ({', '.join(region.weights['project'].get('exclude_use_keywords', []))}) are filtered out.

**Grid.** Distance to the nearest three-phase feeder, plus that feeder's available hosting capacity net of pending DG, from
the utility's hosting-capacity map. ASO and CIP flags are passed through.

**Permitting** comes from the siting tracker (next tab): an active moratorium caps the score, and towns not yet extracted score neutral (0.5).

**Score** = weighted average of seven 0–1 components (sidebar). Hard filters: largest buildable piece ≥ {region.weights['project']['min_contiguous_acres']} ac
({region.weights['project']['min_contiguous_acres_priority']} ac for landfills/brownfields), < 50% protected open space.
""")
    st.markdown("### Data sources")
    src_rows = []
    for k, v in {**region.national, **region.sources}.items():
        if isinstance(v, dict) and ("url" in v or "wcs" in v):
            src_rows.append({"layer": k, "endpoint": v.get("url") or v.get("wcs"), "note": v.get("note", "")})
    for u, v in region.sources.get("hosting_capacity", {}).items():
        src_rows.append({"layer": f"hosting capacity: {u}", "endpoint": v.get("feeders_url") or "— not configured (export from portal → feeders_file)", "note": v.get("portal", "")})
    st.dataframe(pd.DataFrame(src_rows), hide_index=True, use_container_width=True)
    st.markdown("""
### Known limitations
- Screening only. Nothing here replaces a title search, wetland delineation, or an interconnection pre-application report.
- Each parcel is scored alone. Adjacent parcels with the same owner are grouped in the owner list but not merged into one site.
- Feeder geometry is rebuilt from rendered map pixels because the portal doesn't publish vectors. Distances are good to about ±12 m.
- Energy-community data is DOE/NETL's 2024 list. Confirm current-year eligibility and whether the federal credit still applies to the technology.
- NY and OR regions are configured but haven't been run end to end. Several of their layer URLs still need filling in (see `config/sources/`).
""")
