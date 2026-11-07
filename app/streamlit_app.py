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
from reservoir.config import OUTPUTS, list_regions, load_region  # noqa: E402

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


def available_regions() -> list[str]:
    return [rid for rid in list_regions() if (OUTPUTS / rid / "metrics.pkl").exists()]


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
base = outputs.standard_fields(region, scored_parcels(region_id, mtime(region.out_dir / "metrics.pkl"), mtime(tracker.TRACKER)))
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
st.sidebar.caption(f"Run: {meta.get('generated', '?')} · {meta.get('parcels_analysed', len(base)):,} parcels analysed · "
                   f"{int(base.passes_filters.sum()):,} pass filters")

# ------------------------------------------------------------------------------------------- header
st.title(region.name)
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Candidates shown", len(view))
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
        fl = L.get("feeders")
        if fl is not None and len(fl):
            fs = fl.iloc[::6].to_crs(4326)
            g = folium.FeatureGroup("3-phase feeders (color = headroom)")
            for _, r in fs.iterrows():
                hc = r.get("hc_mw")
                col = "#c0392b" if (hc is None or pd.isna(hc) or hc <= 0) else "#f39c12" if hc < 1 else "#27ae60"
                folium.CircleMarker([r.geometry.y, r.geometry.x], radius=1, color=col, fill=True, weight=0, fill_opacity=0.8,
                                    tooltip=f"{r.get('feeder_id')} · {hc if pd.notna(hc) else '?'} MW").add_to(g)
            g.add_to(m)
        sub = L.get("substations")
        if sub is not None and len(sub):
            g = folium.FeatureGroup("Substations")
            for _, r in sub.to_crs(4326).iterrows():
                folium.Marker([r.geometry.y, r.geometry.x], icon=folium.Icon(color="black", icon="bolt", prefix="fa"),
                              tooltip=str(r.get("NAME", "substation"))).add_to(g)
            g.add_to(m)
        cmap = lambda s: "#1a5276" if s >= 75 else "#2e86c1" if s >= 65 else "#85c1e9"
        cols = ["rank", "score", "site_address", "_town", "site_type", "est_mwac", "hc_mw", "feeder_id", "owner_name", "permitting_status"]
        folium.GeoJson(v4[[*cols, "geometry"]], name="Candidate parcels",
                       style_function=lambda f: {"color": cmap(f["properties"]["score"]), "weight": 2, "fillOpacity": 0.25},
                       tooltip=folium.GeoJsonTooltip(cols, aliases=["Rank", "Score", "Address", "Town", "Site type", "MWac", "HC MW", "Feeder", "Owner", "Permitting"])).add_to(m)
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
    show = ["rank", "score", "_town", "site_address", "site_type", "use_desc", "parcel_acres", "buildable_acres",
            "largest_contig_acres", "est_mwac", "feeder_id", "hc_mw", "dist_3ph_m", "substation", "permitting_status",
            "owner_name", *[f"c_{k}" for k in COMPONENTS]]
    t = view[show].copy()
    t["site_flags"] = view["site_flags"].apply(lambda f: " | ".join(f))
    st.dataframe(
        t, hide_index=True, use_container_width=True, height=560,
        column_config={
            "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f"),
            "_town": "Town", "site_address": "Address", "owner_name": "Owner", "use_desc": "Assessor use",
            "parcel_acres": st.column_config.NumberColumn("Parcel ac", format="%.1f"),
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
    d3.caption("Full scored parcel table (pass + fail) is in `outputs/<region>/scored_parcels.gpkg` for QGIS.")

# ------------------------------------------------------------------------------------------- site cards
with tabs[2]:
    if len(view):
        opts = {f"#{int(r['rank'])} · {r['site_address'] or r['parcel_id']}, {r['_town'].title()} · {r.site_type.replace('_', ' ')} · {r.est_mwac:.1f} MWac": i for i, r in view.iterrows()}
        pick = st.selectbox("Parcel", list(opts))
        p = view.loc[opts[pick]]
        html = outputs.CARD.render(**outputs.card_context(region, p, L), standalone=True)
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
**Candidates.** Assessor parcels ≥ {region.weights['project']['min_parcel_acres']} ac in {', '.join(t.title() for t in towns)}.

**Buildable area** = parcel − {region.weights['setbacks_m']['property_line']} m property-line setback − wetlands (NWI + MassDEP)
+ {region.weights['setbacks_m']['wetland_buffer']} m buffer − FEMA SFHA − protected open space (perpetual/temporary) − slope > {region.weights['setbacks_m']['slope_max_pct']}% (3DEP 10 m)
− existing buildings + {region.weights['setbacks_m'].get('building_buffer', 10)} m. Capacity ≈ largest contiguous piece ÷ {region.weights['project']['acres_per_mwac']} ac/MWac, capped at {region.weights['project']['max_mwac']} MW.

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
