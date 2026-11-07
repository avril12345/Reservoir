"""Structured fields for a municipality's solar / BESS siting rules.

Every extracted value carries a verbatim `quote` and a `section` reference so a human can verify it
in seconds and so we can mechanically check the quote exists in the source (the single best guard
against LLM hallucination in this task).
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Permission = Literal["by_right", "site_plan_review", "special_permit", "prohibited", "not_addressed", "unknown"]
MoratoriumStatus = Literal["proposed", "active", "extended", "expired", "repealed", "unknown"]


class Evidence(BaseModel):
    quote: str = Field(description="Verbatim text copied from the source that supports the value. Empty if not found.")
    section: str = Field(description="Section / article number or page where the quote appears, e.g. '§ 9.4.3' or 'p. 12'.")


class UseRule(BaseModel):
    permission: Permission = Field(description="Most permissive pathway for a ground-mounted facility of DG scale (~1–5 MWac) in any district.")
    districts_allowed: list[str] = Field(description="Zoning districts where that pathway applies.")
    max_size_text: str = Field(description="Size cap as written (e.g. '250 kW', '10 acres', 'Tier 2 > 600 kWh prohibited'); empty if none.")
    max_size_mw: float | None = Field(description="Size cap normalised to MWac (solar) or MWh (BESS) if stated; null if none or not convertible.")
    evidence: Evidence


class Setbacks(BaseModel):
    front_ft: float | None
    side_ft: float | None
    rear_ft: float | None
    from_residence_ft: float | None = Field(description="Setback from residential property lines/dwellings if separately specified.")
    evidence: Evidence


class Decommissioning(BaseModel):
    required: bool | None
    financial_surety_required: bool | None
    evidence: Evidence


class Moratorium(BaseModel):
    technology: Literal["solar", "bess", "both"]
    local_law: str = Field(description="Local law / article identifier, e.g. 'Local Law No. 1 of 2026'.")
    status: MoratoriumStatus
    adopted: date | None
    effective: date | None
    expires: date | None
    duration_days: int | None
    scope: str = Field(description="What is paused (applications, permits, approvals) and any size threshold.")
    evidence: Evidence


class ExtractionResult(BaseModel):
    """What the LLM returns for one document."""
    municipality: str
    state: Literal["MA", "NY", "OR"]
    document_title: str
    document_date: date | None = Field(description="Adoption or last-amended date of this document, if stated.")
    solar_ground_mount: UseRule
    bess: UseRule
    solar_setbacks: Setbacks
    bess_setbacks: Setbacks
    decommissioning: Decommissioning
    moratoria: list[Moratorium]
    # MA-specific (2024 Climate Act / 225 CMR 29.00)
    ma_doer_model_solar_bylaw: bool | None = Field(description="MA only: document states it adopts/is based on DOER's model solar bylaw.")
    ma_doer_model_bess_bylaw: bool | None = Field(description="MA only: document states it adopts/is based on DOER's model BESS bylaw.")
    ma_consolidated_permit_bylaw: bool | None = Field(description="MA only: document establishes a consolidated local permit process for small clean energy infrastructure (225 CMR 29.00).")
    notes: str = Field(description="Ambiguities, conflicts, or things a human should double-check.")


class TownRecord(BaseModel):
    """One row of the tracker: the latest known status for a municipality, plus provenance."""
    state: Literal["MA", "NY", "OR"]
    municipality: str
    county: str = ""
    solar_permission: Permission = "unknown"
    bess_permission: Permission = "unknown"
    solar_max_size_text: str = ""
    bess_max_size_text: str = ""
    solar_setback_ft: float | None = None
    bess_setback_ft: float | None = None
    decommissioning_required: bool | None = None
    moratoria: list[Moratorium] = []
    ma_doer_model_solar_bylaw: bool | None = None
    ma_doer_model_bess_bylaw: bool | None = None
    ma_consolidated_permit: Literal["adopted_local_bylaw", "state_default", "unknown", "n/a"] = "unknown"
    sources: list[dict] = Field(default_factory=list, description="[{title, url, retrieved, kind}]")
    method: Literal["llm_extracted", "web_research_seed", "manual"] = "manual"
    quote_check: dict = Field(default_factory=dict, description="field -> 'verbatim' | 'not_found' (mechanical check of LLM quotes)")
    verified_by: str = ""
    verified_on: date | None = None
    notes: str = ""
    updated: date | None = None

    def active_moratoria(self, on: date | None = None) -> list[Moratorium]:
        on = on or date.today()
        out = []
        for m in self.moratoria:
            if m.status in ("expired", "repealed"):
                continue
            if m.status == "proposed":
                continue
            if m.expires and m.expires < on and m.status != "extended":
                continue
            out.append(m)
        return out
