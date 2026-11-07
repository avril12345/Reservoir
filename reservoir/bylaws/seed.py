"""Seed records for the tracker, built from web research on 2026-11-06.

These are NOT human-verified (verified_by is empty); every claim cites where it came from and what
could not be confirmed. Run `reservoir bylaws seed` to (re)write them into tracker.jsonl without
touching other records.
"""
from __future__ import annotations

from datetime import date

from .schema import Evidence, Moratorium, TownRecord
from .tracker import load, save, key

TODAY = date(2026, 11, 6)
CLP_NOTE = ("225 CMR 29.00: local permitting authorities may accept Consolidated Local Permit applications from "
            "2026-07-01 and must by 2026-10-01 for small clean energy infrastructure (<25 MW generation, <100 MWh storage). "
            "Town-level bylaw adoption not yet checked.")
MA_SOURCES = [
    {"title": "Clean Energy Siting & Permitting Regulations (DOER)", "url": "https://www.mass.gov/info-details/clean-energy-siting-permitting-regulations", "retrieved": "2026-11-06", "kind": "state"},
    {"title": "DOER Solar PV & BESS model bylaws", "url": "https://www.mass.gov/doc/solar-pv-battery-energy-storage-system-bess-model-bylaws/download", "retrieved": "2026-11-06", "kind": "state"},
    {"title": "MMA: Administration finalizes streamlined energy siting, permitting process", "url": "https://www.mma.org/administration-finalizes-streamlined-energy-siting-permitting-process/", "retrieved": "2026-11-06", "kind": "news"},
]


def seeds() -> list[TownRecord]:
    recs = [
        TownRecord(
            state="NY", municipality="Waterford", county="Saratoga",
            moratoria=[Moratorium(
                technology="bess", local_law="Local Law No. 1 of 2026", status="active",
                adopted=date(2026, 6, 2), effective=None, expires=None, duration_days=180,
                scope="Battery Energy Storage Systems Facilities; 180 days from the effective date.",
                evidence=Evidence(quote="establishing a moratorium on Battery Energy Storage Systems Facilities", section="LL 1-2026 (title)"),
            )],
            sources=[
                {"title": "Local Law No. 1 of 2026 (adopted June 2, 2026) – 180-day moratorium BESS", "url": "https://www.townofwaterfordny.gov/town-board/files/local-law-1-2026-adopted-june-2-2026-180-day-moratorium-battery-energy-storage", "retrieved": "2026-11-06", "kind": "primary (403 to automated fetch; facts from search index)"},
                {"title": "Town Board public hearing – extending the BESS moratorium", "url": "https://www.townofwaterfordny.gov/home/events/7186", "retrieved": "2026-11-06", "kind": "primary (403 to automated fetch)"},
            ],
            method="web_research_seed",
            notes=("Effective date = filing with NY Secretary of State (not confirmed), so 180-day expiry is roughly early Dec 2026. "
                   "Hearing held 2026-10-06 on a further 180-day extension; outcome not confirmed."),
            updated=TODAY,
        ),
        TownRecord(
            state="NY", municipality="Cortlandville", county="Cortland",
            moratoria=[
                Moratorium(technology="solar", local_law="Solar moratorium (2nd extension, +60 days)", status="unknown",
                           adopted=None, effective=None, expires=None, duration_days=60,
                           scope="Solar development; extended to bridge until new local laws are enacted.",
                           evidence=Evidence(quote="unanimously approved a 60-day extension of two moratoriums—one on solar development and another on battery energy storage systems (BESS)", section="WXHC, 2026-04-17")),
                Moratorium(technology="bess", local_law="BESS moratorium (+60 days)", status="unknown",
                           adopted=None, effective=None, expires=None, duration_days=60,
                           scope="Battery energy storage systems.",
                           evidence=Evidence(quote="unanimously approved a 60-day extension of two moratoriums—one on solar development and another on battery energy storage systems (BESS)", section="WXHC, 2026-04-17")),
            ],
            sources=[
                {"title": "WXHC: Cortlandville extends ban on solar & battery projects for another 60 days", "url": "https://www.wxhc.com/cortlandville-extends-ban-on-solar-battery-projects-for-another-60-days/", "retrieved": "2026-11-06", "kind": "news"},
                {"title": "Proposed Local Law of 2026 – Battery Energy Storage Systems", "url": "https://www.cortlandville.gov/DocumentCenter/View/864/Proposed-Local-Law-of-2026---Battery-Energy-Storage-Systems", "retrieved": "2026-11-06", "kind": "primary (now 404; text seen in search index only)"},
                {"title": "Existing BESS law, Local Law 3-2022 (eCode360)", "url": "https://ecode360.com/39045023", "retrieved": "2026-11-06", "kind": "primary"},
            ],
            method="web_research_seed",
            notes=("Proposed 2026 BESS law reportedly prohibits Tier 2 BESS in all zoning districts (search-index text; document now 404). "
                   "Public hearing on new solar & BESS laws was 2026-05-06; adoption not confirmed. Under LL 3-2022, Tier 2 (>600 kWh) needed a special use permit."),
            updated=TODAY,
        ),
        TownRecord(
            state="NY", municipality="Cortlandt", county="Westchester",
            bess_permission="not_addressed",
            moratoria=[Moratorium(
                technology="bess", local_law="Local Law No. 1 of 2026", status="extended",
                adopted=date(2026, 7, 21), effective=None, expires=date(2026, 12, 31), duration_days=None,
                scope="No application considered, nor approvals granted, for new commercial BESS (Planning Board, ZBA, Code Enforcement).",
                evidence=Evidence(quote="The moratorium herein shall be extended through December 31, 2026 unless terminated earlier.", section="LL 1-2026 § 3"),
            )],
            sources=[{"title": "Resolution 168-26 adopting Local Law No. 1 of 2026", "url": "https://www.townofcortlandtny.gov/documents/RESOLUTION-ADOPTLL2026-01-168-2026.pdf", "retrieved": "2026-11-06", "kind": "primary", "local_copy": "data/bylaws/docs/ny_cortlandt_ll1_2026.txt"}],
            quote_check={"moratoria[0]": "verbatim"},
            method="web_research_seed",
            notes=("Not Cortlandville. Kept as an entity-confusion test case: search results mix the two towns. "
                   "Code currently 'devoid of provisions directly related to Battery Energy Storage' (§ 1). Prior extensions: LL 2-2024, 10-2024, 2-2025, 7-2025."),
            updated=TODAY,
        ),
    ]
    for town in ("Uxbridge", "Douglas", "Northbridge", "Sutton"):
        recs.append(TownRecord(state="MA", municipality=town, county="Worcester", ma_consolidated_permit="unknown",
                               sources=MA_SOURCES, method="web_research_seed", notes=CLP_NOTE, updated=TODAY))
    return recs


def write_seeds() -> int:
    recs = load()
    for r in seeds():
        k = key(r.state, r.municipality)
        if k in recs and recs[k].verified_by:
            continue  # never overwrite a human-verified record with seed data
        recs[k] = r
    save(recs)
    return len(recs)
