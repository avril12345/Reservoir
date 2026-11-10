"""LLM extraction of siting rules from a bylaw / local law, with mechanical quote verification.

Design choices aimed at known LLM failure modes:
  * Every field must carry a verbatim quote + section. We then check the quote really occurs in the
    source text (normalised for whitespace/quotes/hyphenation). Unsupported values are demoted to
    "unknown" and recorded in `quote_check` — the model can't silently invent a setback.
  * "not_addressed" is a first-class answer, so the model isn't pushed to fill every slot.
  * Statuses that depend on today's date (is a moratorium still active?) are re-derived in code
    from the extracted dates, not trusted from the model.
  * The whole document goes in — no truncation. Long codes (100+ pages) fit in the 1M context and the
    document block is prompt-cached so re-running or asking follow-ups is cheap.
"""
from __future__ import annotations

import difflib
import io
import json
import re
import unicodedata
from datetime import date
from pathlib import Path

import anthropic
import requests

from .schema import ExtractionResult, TownRecord
from . import tracker

MODEL = "claude-opus-5-5"

SYSTEM = """You extract municipal land-use rules for distributed solar and battery energy storage (BESS) projects \
from zoning bylaws, ordinances, and local laws, for a developer's site-screening tracker.

Target project: ground-mounted solar of roughly 1–5 MWac, optionally paired with a BESS of a few MWh.

Rules:
- Use only the supplied document. If it does not address something, answer "not_addressed" (permissions) or null.
- Every evidence.quote must be copied character-for-character from the document (it will be checked by code). \
Keep quotes short: the one sentence or clause that supports the value. Give the section number or page in evidence.section.
- permission = the most permissive pathway available for a DG-scale facility in any district: by_right, \
site_plan_review (as-of-right with site plan approval), special_permit (special use / special permit / conditional use), \
or prohibited. If small systems are allowed by right but DG-scale needs a special permit, answer special_permit.
- NY BESS laws based on the NYSERDA model use tiers (Tier 1 small, Tier 2 larger, sometimes Tier 3). \
Report the pathway for the tier that a multi-MWh system falls in, and copy the tier threshold into max_size_text.
- Moratoria: record each one with exact dates as written. Do not infer an expiry that is not stated; \
if only a duration is given ("180 days from the effective date") give duration_days and leave expires null.
- Keep units as written in max_size_text; only fill max_size_mw when the conversion is unambiguous (kW->MW). \
Flag AC vs DC ambiguity in notes.
- If the document is about a different municipality than the one named in the request, say so in notes."""


# ------------------------------------------------------------------------------------- source text
def load_source(src: str) -> tuple[str, str]:
    """Return (text, source_label) from a URL, PDF, HTML or text file."""
    if re.match(r"^https?://", src):
        r = requests.get(src, timeout=120, headers={"User-Agent": "reservoir-screener/0.1"})
        r.raise_for_status()
        data, ctype = r.content, r.headers.get("content-type", "")
        is_pdf = data[:4] == b"%PDF" or "pdf" in ctype
        is_html = "html" in ctype
    else:
        data = Path(src).read_bytes()
        is_pdf = data[:4] == b"%PDF"
        is_html = src.lower().endswith((".html", ".htm"))
    if is_pdf:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join(f"[page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))
        if len(text.strip()) < 200:
            raise ValueError("PDF has little or no text layer (scanned?) — OCR it first.")
    elif is_html:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(data, "html.parser")
        for t in soup(["script", "style", "nav", "footer"]):
            t.decompose()
        text = soup.get_text("\n")
    else:
        text = data.decode("utf-8", errors="replace")
    return re.sub(r"\n{3,}", "\n\n", text), src


# ------------------------------------------------------------------------------------- quote check
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-")
    s = re.sub(r"-\s*\n\s*", "", s)          # de-hyphenate line breaks
    s = re.sub(r"[^\w%§.$'\"-]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def check_quote(quote: str, source_norm: str) -> str:
    q = _norm(quote)
    if not q:
        return "empty"
    if q in source_norm:
        return "verbatim"
    # PDF text layers often drop or double spaces; compare with all whitespace removed.
    if q.replace(" ", "") in source_norm.replace(" ", ""):
        return "verbatim"
    m = difflib.SequenceMatcher(None, source_norm, q, autojunk=False).find_longest_match(0, len(source_norm), 0, len(q))
    return "near_verbatim" if m.size / len(q) >= 0.85 else "not_found"


def verify(result: ExtractionResult, source_text: str) -> dict[str, str]:
    src = _norm(source_text)
    checks = {
        "solar_ground_mount": check_quote(result.solar_ground_mount.evidence.quote, src),
        "bess": check_quote(result.bess.evidence.quote, src),
        "solar_setbacks": check_quote(result.solar_setbacks.evidence.quote, src),
        "bess_setbacks": check_quote(result.bess_setbacks.evidence.quote, src),
        "decommissioning": check_quote(result.decommissioning.evidence.quote, src),
    }
    for i, m in enumerate(result.moratoria):
        checks[f"moratoria[{i}]"] = check_quote(m.evidence.quote, src)
    return checks


# ------------------------------------------------------------------------------------- LLM call
def extract(text: str, state: str, municipality: str, client: anthropic.Anthropic | None = None) -> ExtractionResult:
    client = client or anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                # Document first and cached: re-extraction or follow-up questions on the same code hit the cache.
                {"type": "text", "text": f"<document>\n{text}\n</document>", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"Municipality: {municipality}, {state}. Today is {date.today().isoformat()}. Extract the siting fields."},
            ],
        }],
        output_format=ExtractionResult,
        # Server-side refusal fallback (beta): if a safety classifier declines, the API retries on a
        # fallback model inside the same call instead of returning a bare refusal.
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined: {getattr(response.stop_details, 'category', None)}")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"Extraction incomplete (stop_reason={response.stop_reason}, request {response._request_id})")
    return response.parsed_output


def to_record(result: ExtractionResult, checks: dict[str, str], source: str, county: str = "", prior: TownRecord | None = None) -> TownRecord:
    ok = lambda k: checks.get(k) in ("verbatim", "near_verbatim")
    today = date.today()
    moratoria = []
    for i, m in enumerate(result.moratoria):
        if not ok(f"moratoria[{i}]"):
            continue                                   # unsupported moratorium claims are dropped, not trusted
        if m.expires and m.expires < today and m.status in ("active", "unknown"):
            m = m.model_copy(update={"status": "expired"})
        moratoria.append(m)
    setback = lambda s: max([v for v in (s.front_ft, s.side_ft, s.rear_ft) if v is not None], default=None)
    rec = TownRecord(
        state=result.state, municipality=result.municipality, county=county or (prior.county if prior else ""),
        solar_permission=result.solar_ground_mount.permission if ok("solar_ground_mount") or result.solar_ground_mount.permission == "not_addressed" else "unknown",
        bess_permission=result.bess.permission if ok("bess") or result.bess.permission == "not_addressed" else "unknown",
        solar_max_size_text=result.solar_ground_mount.max_size_text if ok("solar_ground_mount") else "",
        bess_max_size_text=result.bess.max_size_text if ok("bess") else "",
        solar_setback_ft=setback(result.solar_setbacks) if ok("solar_setbacks") else None,
        bess_setback_ft=setback(result.bess_setbacks) if ok("bess_setbacks") else None,
        from_residence_ft=result.solar_setbacks.from_residence_ft if ok("solar_setbacks") else None,
        decommissioning_required=result.decommissioning.required if ok("decommissioning") else None,
        moratoria=moratoria,
        ma_doer_model_solar_bylaw=result.ma_doer_model_solar_bylaw,
        ma_doer_model_bess_bylaw=result.ma_doer_model_bess_bylaw,
        ma_consolidated_permit=("adopted_local_bylaw" if result.ma_consolidated_permit_bylaw else "unknown") if result.state == "MA" else "n/a",
        sources=[*(prior.sources if prior else []), {"title": result.document_title, "url": source, "retrieved": today.isoformat(), "kind": "llm_input"}],
        method="llm_extracted", quote_check=checks, notes=result.notes, updated=today,
    )
    return rec


def extract_to_tracker(state: str, municipality: str, source: str, county: str = "", client=None):
    text, label = load_source(source)
    result = extract(text, state, municipality, client)
    checks = verify(result, text)
    prior = tracker.load().get(tracker.key(state, municipality))
    rec = to_record(result, checks, label, county, prior)
    # Keep every raw extraction for the accuracy audit, regardless of what goes into the tracker.
    audit = tracker.TRACKER.parent / "extractions"
    audit.mkdir(exist_ok=True)
    slug = re.sub(r"\W+", "_", municipality)
    stem = f"{state}_{slug}_{date.today().isoformat()}"
    payload = {"source": label, "checks": checks, "result": result.model_dump(mode="json")}
    (audit / f"{stem}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Never overwrite a human-verified record with fresh LLM output; the reviewer compares manually.
    if not (prior and prior.verified_by):
        tracker.upsert(rec)
    return rec, result
