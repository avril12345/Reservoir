"""The Local Siting & Moratorium Tracker store: one JSON line per municipality (data/bylaws/tracker.jsonl)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..config import ROOT
from .schema import TownRecord

TRACKER = ROOT / "data" / "bylaws" / "tracker.jsonl"


def key(state: str, municipality: str) -> str:
    m = municipality.upper().replace("TOWN OF ", "").replace("CITY OF ", "").strip()
    return f"{state.upper()}|{m}"


def load(path: Path = TRACKER) -> dict[str, TownRecord]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = TownRecord.model_validate_json(line)
            out[key(r.state, r.municipality)] = r
    return out


def save(records: dict[str, TownRecord], path: Path = TRACKER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r.model_dump_json() for _, r in sorted(records.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def upsert(record: TownRecord, path: Path = TRACKER) -> None:
    recs = load(path)
    recs[key(record.state, record.municipality)] = record
    save(recs, path)


def permitting_assessment(rec: TownRecord | None, scores: dict, on: date | None = None) -> tuple[float, list[str], str]:
    """Return (0..1 score, flags, status label) for a solar+storage project in this town."""
    if rec is None:
        return scores["unknown"], ["Permitting: town not yet in siting tracker"], "unknown"
    flags = []
    active = rec.active_moratoria(on)
    solar_m = [m for m in active if m.technology in ("solar", "both")]
    bess_m = [m for m in active if m.technology in ("bess", "both")]
    s_solar = scores.get(rec.solar_permission, scores["unknown"])
    s_bess = scores.get(rec.bess_permission, scores["unknown"])
    if bess_m:
        s_bess = scores["moratorium"]
        flags += [f"BESS moratorium ({m.local_law}, {m.status}" + (f", through {m.expires}" if m.expires else "") + ")" for m in bess_m]
    if solar_m:
        s_solar = scores["moratorium"]
        flags += [f"Solar moratorium ({m.local_law}, {m.status}" + (f", through {m.expires}" if m.expires else "") + ")" for m in solar_m]
    if rec.bess_permission == "prohibited":
        flags.append("BESS prohibited at this scale — solar-only or relocate storage")
    if rec.solar_permission == "prohibited":
        flags.append("Ground-mount solar prohibited at this scale")
    for m in rec.moratoria:
        if m.status == "proposed":
            flags.append(f"Proposed {m.technology.upper()} moratorium ({m.local_law}) — watch")
        elif m.expires and m.expires < (on or date.today()) and m.status not in ("expired", "repealed"):
            flags.append(f"{m.technology.upper()} moratorium {m.local_law} past stated expiry {m.expires} — check for extension")
    if rec.solar_permission == "unknown" and not active:
        flags.append(f"Permitting: {rec.municipality} solar/BESS bylaw not yet extracted")
    score = 0.7 * s_solar + 0.3 * s_bess
    if solar_m:
        score = min(score, scores["moratorium"])
    label = "moratorium" if (solar_m or bess_m) else rec.solar_permission
    if not rec.verified_by:
        flags.append("Tracker record not yet human-verified")
    return score, flags, label
