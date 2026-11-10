# Reservoir Site Screener + Local Siting & Moratorium Tracker

Open-source tools for originating distributed-generation (DG) solar + storage sites by direct site search,
built for a two-analyst origination team:

1. **Site Screener** (MA first, NY and OR configured): a Python/GeoPandas pipeline that scores every site in a
   utility territory or set of towns. A site is one tax lot, or an assemblage of same-owner neighbouring lots. Scoring leans toward **landfills, brownfields, gravel pits and industrial
   land** (the East Light pattern and SMART 3.0's brownfield/landfill priority). It produces a ranked shortlist,
   a one-page **site card** per parcel, and an **owner list** for outreach.
2. **Local Siting & Moratorium Tracker**, an add-on to #1. Claude extracts structured fields from zoning bylaws and
   local laws: allowed by right vs special permit, size caps, setbacks, decommissioning, and moratorium dates. Each
   field carries a verbatim quote that code checks against the source. The tracker feeds a permitting score back
   into #1. A **20-town human-verified accuracy check** measures field-level error rates by error type.

Both tools run in one tabbed Streamlit interface: Map · Shortlist · Site cards · Owner outreach · Siting tracker ·
Accuracy check · Method & sources.

```
uv sync
uv run reservoir run --region ma_ngrid_blackstone     # fetch (~3 min first time, cached after) + score
uv run streamlit run app/streamlit_app.py             # open the interface
uv run pytest                                          # 26 tests, offline
```

`uv` installs Python 3.12 and all dependencies. No GIS install or API key is needed for the screener.

---

## 1 · Site Screener

### Pipeline

```
config/regions/*.yaml ──► fetch.py ─────────────► outputs/<region>/inputs.gpkg  (+ cached rasters)
  towns or utility          per town: parcels,      every layer in the state plane CRS,
  territory                 constraints, overlays,  open it in QGIS to audit
                            feeders, 3DEP, NLCD
                                   │
                                   ▼
                         analysis.py ─────────────► lots → same-owner assemblages → dissolved sites;
                                   │                buildable area, site type, constraint shares,
                                   │                nearest 3-phase feeder + hosting capacity
                                   ▼
          bylaws/tracker ──► scoring.py ──────────► 0–100 score from 7 components + hard filters + flags
                                   │
                                   ▼
                         outputs.py ──────────────► shortlist.csv/.geojson, scored_parcels.gpkg,
                                                    owner_list.csv, cards/*.html (print → PDF)
```

### Layers (Massachusetts, all checked live 2026-11-06)

| Purpose | Layer | Source |
|---|---|---|
| Parcels + owners | Property Tax Parcels (L3, statewide) | MassGIS |
| Roads (splits assemblages) | MassDOT Road Inventory, all jurisdictions | MassDOT / MassGIS |
| Towns + utility territory | Electricity Providers by Town | MassGIS |
| Wetlands | National Wetlands Inventory + MassDEP Wetlands | USFWS, MassDEP |
| Flood | NFHL Flood Hazard Zones (SFHA: A, AE, AH, AO, AR, A99, V, VE) | FEMA |
| Slope | 3DEP elevation, 10 m → slope % | USGS |
| Land cover / forest | NLCD 2021 (WCS) | MRLC |
| Protected open space | OpenSpace (LEV_PROT = P or T excluded) | MassGIS |
| Buildings | Building Structures (2-D) | MassGIS |
| Brownfields | Chapter 21E tier-classified sites; AUL sites | MassDEP |
| Landfills | Solid-waste landfill and dumping-ground polygons/points | MassDEP |
| Low-income / EJ | Environmental Justice Populations 2020 (income criterion) | MassGIS / EEA |
| Energy community | 2024 MSA/non-MSA and coal-closure energy communities | DOE / NETL |
| 3-phase feeders + hosting capacity | MA Hosting Capacity with DG Pending (layer 0, 3-phase) | National Grid System Data Portal |
| Substations | MASDP Substations | National Grid System Data Portal |

**Grid data notes.**
- *HIFLD Open went offline in August 2025.* HIFLD transmission lines were also never a source for
  *three-phase distribution*. This tool takes three-phase feeders and their headroom straight from each utility's
  hosting-capacity portal.
- National Grid's portal returns feeder **attributes** (feeder ID, substation, kV, `HC_Available_MW` with and
  without pending DG, ASO and CIP flags, update date) but **withholds vector geometry**. `grid.py` rebuilds each
  feeder's path by exporting a map image filtered to that one feeder (`layerDefs`) at 5 m/pixel. Positions are
  good to about ±12 m, which is fine for screening.
- **Eversource and Unitil** (MA), **NYSEG and Con Ed** (NY), and **PGE and Pacific Power** (OR) need a portal
  export. Set `feeders_file` in `config/sources/<state>.yaml` to a GeoJSON/GPKG with the same field mapping.
  National Grid NY (`NYSDP/Hosting_Capacity_Data`) is already configured.
- Follow each utility portal's terms of use.

### Scoring (`config/weights/default.yaml`, re-weightable live in the app)

| Component | Weight | 0 → 1 |
|---|---|---|
| Site type | 30 | landfill / brownfield 1.0 · gravel pit 0.95 · vacant industrial 0.85 · industrial 0.7 · utility 0.6 · commercial 0.45 · municipal 0.4 · … |
| Feeder headroom | 20 | HC MW ÷ est. project MW, capped at 1. Zero headroom scores 0 and is flagged. |
| 3-phase distance | 15 | 1 at ≤ 150 m, 0 at ≥ 2 km, small bonus within 1.5 km of a substation |
| Size | 10 | buildable MWac vs. 1–5 MW DG band |
| Permitting | 10 | from the tracker: by right 1.0, special permit 0.6, unknown 0.5, active moratorium 0.05, prohibited 0 |
| Incentives | 8 | SMART priority category (landfill/brownfield), energy community, low-income/EJ |
| Land cover | 7 | 1 − forest share of the buildable area |

**Sites and assemblages** (`assemblage:` in the weights file). Lots ≥ 1 ac are grouped into one site when they have the
same owner key (name + mailing address, uppercased, punctuation stripped; never name alone, never placeholders like
OWNER UNKNOWN), sit in the same town, touch or lie within 20 m of each other, and have no mapped road between them.
A public way makes two sites. An unbuilt paper road isn't in the road inventory, so lots across one still join and get
a "check on imagery" flag. Residential, excluded-use and conservation lots never join. The lots are dissolved
(sub-metre digitising slivers closed) **before** the setback and constraints, so interior lot lines don't eat
buildable area. An assemblage is a landfill or brownfield if any member lot is. Otherwise it takes its best member's use.
Matching is exact. Fuzzy owner matching ("SMITH JOHN" vs "SMITH JANE") is not done yet.

**Buildable area** = site − property-line setback − wetlands (+30.5 m / 100 ft buffer) − SFHA − protected open space −
slope > 15% − buildings (+10 m). The property-line setback is the town's `solar_setback_ft` from the siting tracker
when one is stored (verified quote or reviewer entry), otherwise the 15 m (~49 ft) default. It is applied to the dissolved
site, and each site records `setback_ft_applied` and `setback_source`. Residence and BESS setbacks are flagged, not
applied. After a tracker setback change, rerun `reservoir score --reanalyse`. **Hard filters:** largest contiguous buildable piece of the site ≥ 4 ac (2.5 ac for
landfills/brownfields), on sites ≥ 3 ac. Residential, conservation (Article 97), cemetery, golf, school/education and airport land is removed, even if it is on a 21E site.

**Site-type precedence:** residential use wins. Otherwise landfill (MassDEP polygon ≥ 10% of the parcel, or a
point on it) → brownfield (21E or AUL point on the parcel) → gravel pit (DOR 410–412) → disturbed land (≥ 30% NLCD barren on a vacant parcel) →
assessor use code.

### Outputs (`outputs/<region>/`)

- `shortlist.csv` / `shortlist.geojson`: the top 50 passing **sites** (what you originate) with every component and
  flag, plus `n_parcels`, `member_ids` and a summary like "3 lots, same owner, 6.1 ac combined"
- `shortlist_lots.csv`: the tax lots behind those sites (what you buy): one row per lot for title and deed work
- `scored_parcels.gpkg`: layer `sites` (every site, pass and fail) and layer `lots` (every tax lot with its
  `assemblage_id`, site rank and score) for QGIS auditing
- `owner_list.csv`: grouped by owner key, with owner type (public, utility, business, trust,
  individual), absentee flag, sites, lots, acres, MW, best rank
- `cards/card_NNN_*.html` and `cards/all_cards.html`: one-page site cards (map with tax-lot lines, acreage, buildable area, feeder,
  hosting capacity, permitting, flags, owner). Print to PDF for one page per site.

### Region status

| Region | Status |
|---|---|
| `ma_ngrid_blackstone`: National Grid MA, Uxbridge / Douglas / Northbridge / Sutton | **Run end to end.** Set `all_utility_towns: true` for all 159 National Grid MA towns. |
| `ny_hudson_valley` | Configured. NY parcel and National Grid NY hosting-capacity endpoints respond. NYSDEC brownfield, landfill, NYPAD and DAC layer URLs and the NY parcel field names still need confirming. |
| `or_pge` | Scaffold only (national layers work; state parcels, cleanup sites and utility HC still to add). |

---

## 2 · Local Siting & Moratorium Tracker

`data/bylaws/tracker.jsonl` holds one record per municipality (`reservoir/bylaws/schema.py`). Each record has the
solar and BESS permission pathway, size caps, setbacks, decommissioning, moratoria (law, status, adopted,
effective, expires), MA-specific fields (DOER model solar/BESS bylaw adoption, consolidated local permit status),
sources, extraction method, quote checks and verification.

```
uv run reservoir bylaws extract --state NY --town Cortlandt --source data/bylaws/docs/ny_cortlandt_ll1_2026.txt
uv run reservoir bylaws extract --state MA --town Uxbridge --source https://…/zoning-bylaw.pdf
```

Extraction calls `claude-opus-5-5` with structured outputs (a Pydantic schema). The full document is sent with
no truncation and is prompt-cached. Server-side refusal fallback is enabled. Guards against known LLM failure
modes:

- **Every field needs a verbatim quote and a section.** `extract.verify()` normalizes whitespace, smart quotes
  and PDF hyphenation, then checks each quote is actually in the source. A field whose quote isn't found is
  downgraded to `unknown`, and a moratorium whose quote isn't found is dropped.
- **Dates decide status.** Whether a moratorium is still active is re-derived in code from its dates. Past-expiry
  moratoria are flagged with "check for extension".
- **Proposed laws don't count.** A proposed law never changes a score.
- **Human records are protected.** LLM output never overwrites a human-verified record. Every raw extraction is
  saved to `data/bylaws/extractions/`.

**MA 2026 changes:** under 225 CMR 29.00, local permitting authorities could begin accepting Consolidated Local
Permit applications for small clean energy infrastructure (< 25 MW, < 100 MWh) on July 1, 2026 and **must** by
Oct 1, 2026. DOER also published updated model solar and new model BESS bylaws. The tracker records each town's
status on all three. Pilot towns are seeded as `unknown` until their bylaws are extracted.

**Seed records (web research 2026-11-06, not yet human-verified):**

| Town | What the record says | Confidence |
|---|---|---|
| Waterford, NY (Saratoga) | Local Law 1 of 2026, adopted 6/2/2026: 180-day BESS moratorium. Hearing 10/6/2026 on another 180 days. | Primary pages block automated fetch, so these facts come from the search index. Outcome of the extension isn't confirmed. |
| Cortlandville, NY (Cortland) | Solar and BESS moratoria extended 60 days (WXHC, 4/17/2026). The proposed 2026 BESS law reportedly **prohibits Tier 2 in all districts**. Hearing was 5/6/2026. | Adoption not confirmed, and the proposed-law PDF now returns 404. Recorded as *proposed*. |
| Cortlandt, NY (Westchester) | Local Law 1 of 2026, adopted 7/21/2026, extends the BESS moratorium **through 12/31/2026**. | From the primary PDF, quote verified. Kept as an **entity-confusion test**: search results mix it up with Cortlandville. |

### Finding the source document: discover → a person picks → extract → verify

```
uv run reservoir bylaws discover --state MA --town Uxbridge --county Worcester [--seed-url URL ...]
uv run reservoir bylaws extract  --state MA --town Uxbridge --from-discover 1    # or --source <URL you chose>
```

`discover` collects candidate documents from several places:
- primary URLs already in the tracker
- guessed town hosts and their planning, zoning and DocumentCenter pages
- code hosts (eCode360, Municode, General Code)
- links up to two hops from those pages (the second hop stays on the same host)
- `--seed-url`s you add
- the Brave Search API, only if `BRAVE_SEARCH_API_KEY` is set (it never scrapes a search engine)

Every query names the state and county. Each document is fetched politely and cached in
`data/bylaws/discover_cache/`, including 403/404 errors, so nothing is retried in a loop. Documents are read the same
way `extract` reads them, then classified (living code, clerk PDF, local law, news, staff report, proposed, model bylaw).

**Set aside, never offered for extraction:**
- dead links (403/404, host not found)
- documents that name a different municipality, e.g. a Cortlandville law when you asked for Cortlandt
- proposed laws, drafts and hearing notices
- DOER, NYSERDA and MMA model bylaws
- search, index and login pages
- scanned PDFs with no text layer

News stays in the list, ranked last, and can't be extracted.

The top five go to `data/bylaws/discover/<STATE>_<Town>.json`, which also records new URLs since the last run.
Ranking order: living code > recent town PDF > dated local law > other town pages > news.

**Discovery never writes the tracker.** Permitting scores and setbacks change only after a person picks a candidate
and `extract` runs its quote check. Human-verified records are never overwritten. The Siting tracker tab has the same
flow under **Find likely sources**.

Live check, 2026-11-07, with no search key:
- **Uxbridge, MA:** ranked the town's *Zoning By-Laws* PDF first.
- **Cortlandt, NY:** eCode360's Town of Cortlandt code, then the adopted LL 1-2026.
- **Cortlandville, NY:** eCode360 *Chapter 64: Battery Energy Storage Systems*. No cross-town matches.

### Accuracy check (20 towns, hand-checked)

```
uv run reservoir bylaws worksheet   # data/bylaws/verification_worksheet.csv (tracker values pre-filled)
# reviewer fills the truth_* columns from the primary source → save as data/bylaws/ground_truth.csv
uv run reservoir bylaws accuracy    # per-field accuracy, Wilson 95% interval, error taxonomy
```

The sample has 13 National Grid MA towns (pilot plus the next ring) and 7 NY towns (the 3 seeds plus 4 Hudson
Valley towns), stratified by state and by whether a moratorium exists. Errors are classified as *omission*,
*hallucination*, *wrong value*, *stale*, *entity confusion* or *unit*. The first three are detected
automatically. The reviewer tags the rest. **No ground truth ships with the repo.** The accuracy tab shows 0/20
until a person does the check, because a made-up accuracy number would defeat the purpose.

---

## Loom outline (≈4 min)

1. Problem (20 s): a two-analyst team originating DG sites by direct search. Permitting risk is what kills
   small projects.
2. Map tab (60 s): National Grid Blackstone Valley pilot. Feeders colored by headroom. Turn on imagery and zoom
   to the top-ranked vacant industrial parcel, then to the capped Uxbridge Landfill (0 MW headroom, Article 97
   flag).
3. Re-weight live (30 s): raise *feeder headroom*, watch the gravel pits on 0-MW feeders drop.
4. Site card + owner list (40 s): print a card to PDF, export the mail-merge CSV.
5. Siting tracker (60 s): Cortlandt vs Cortlandville. Extract a local law live and show the quote check catching
   an unsupported value.
6. Accuracy tab (30 s): the worksheet, the error taxonomy, and why the number stays blank until a person checks
   20 towns.

## Limitations

Screening only. This doesn't replace title work, a wetland delineation, or a utility pre-application report.
Assemblages need an exact owner-name and mailing-address match, so related owners (family members, affiliated
LLCs) aren't joined. NY and OR have no roads layer configured yet, so lots there can still join across a public road (they are flagged). Assessor use codes
and owner records can lag by a year. Energy-community eligibility and federal credit applicability need a
current-year check.

## Walkthrough video

```
uv run --with playwright --with imageio-ffmpeg python tools/demo_video.py
```

Records a narrated walkthrough to `outputs/video/reservoir_walkthrough.mp4`, plus `narration_script.md` and a `.srt`
subtitle file. Narration numbers and site names come from the latest run. The voice is the built-in Windows voice
(`--voice "Microsoft David Desktop"` to change it). The app runs with `RESERVOIR_DEMO_REDACT=1`, which masks
individual and trust owners' names and mailing addresses; businesses and public owners stay visible.
