"""Bylaw candidate finder: suggest the documents a person should pick from, never pick for them.

For one municipality: build search strings, try direct URL patterns (town sites, code hosts, links one hop
from them, optional web search, analyst seed URLs), fetch what opens, classify each document, reject the
known junk (wrong town, drafts and hearing notices, state model bylaws, dead links), rank the rest, and
write a queue file. A person then runs `reservoir bylaws extract --from-discover N`, which goes through the
usual extract + quote check. Nothing here writes to the tracker.

    reservoir bylaws discover --state NY --town Cortlandt --county Westchester
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

import requests
from pydantic import BaseModel, Field

from . import extract, tracker

USER_AGENT = "reservoir-screener/0.1"
CODE_HOSTS = ("ecode360.com", "library.municode.com", "www.generalcode.com", "generalcode.com")
NEWS_HOSTS = ("wxhc.com", "mma.org", "adirondackexplorer.org", "patch.com", "now.solar", "newsbreak.com", "citizenportal.ai")
MODEL_ISSUERS = ("mass.gov", "nyserda.ny.gov", "mma.org")
STATE_NAMES = {"MA": "Massachusetts", "NY": "New York", "OR": "Oregon"}
DELAY_S = 1.0            # pause between live requests to the same run
TIMEOUT_S = (8, 30)      # connect, read
MAX_FETCHES = 40         # per town, across patterns, one-hop links and search results
MAX_SHOWN = 5

Kind = Literal["living_code", "clerk_pdf", "local_law", "news", "staff_report", "proposed", "model_bylaw", "unknown"]
Match = Literal["exact", "likely", "mismatch", "unknown"]

# Pairs that search results confuse. A document naming the other one is a mismatch, never "likely".
CONFUSABLE = {
    "cortlandt": ["cortlandville", "cortland"],
    "cortlandville": ["cortlandt", "cortland"],
    "cortland": ["cortlandt", "cortlandville"],
}


def cache_dir() -> Path:
    return tracker.TRACKER.parent / "discover_cache"


def queue_dir() -> Path:
    return tracker.TRACKER.parent / "discover"


class Candidate(BaseModel):
    url: str
    source: str = Field(description="seed | tracker | pattern | link | search")
    http_status: int | None = None
    content_type: str = ""
    title: str = ""
    retrieved: str = ""
    kind: Kind = "unknown"
    municipality_match: Match = "unknown"
    reject_reason: str = ""
    scanned: bool = False
    year: int | None = None
    excerpt: str = ""
    score: float = 0.0
    rank: int | None = None

    @property
    def extractable(self) -> bool:
        return not self.reject_reason and self.kind != "news"


class DiscoverResult(BaseModel):
    state: str
    municipality: str
    county: str = ""
    queries: list[str]
    searched_web: bool
    retrieved: str
    candidates: list[Candidate] = Field(description="Ranked survivors shown to the analyst (max 5)")
    more: list[Candidate] = Field(default_factory=list, description="Survivors beyond the top 5")
    rejects: list[Candidate] = Field(default_factory=list)
    previous_run: str | None = Field(default=None, description="Timestamp of the queue this run replaced")
    new_urls: list[str] = Field(default_factory=list, description="URLs not seen in the previous run (empty on a first run)")


# ------------------------------------------------------------------------------------------- names and queries
def norm_town(name: str) -> str:
    n = re.sub(r"^\s*(town|city|village|township)\s+of\s+", "", str(name), flags=re.I)
    return re.sub(r"\s+", " ", n).strip().casefold()


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm_town(name))


def build_queries(state: str, town: str, county: str = "", year: int | None = None) -> list[str]:
    """Plain search strings. Every query names the state (and county) so Cortlandt, NY isn't Cortlandville."""
    t = f'"{town.title()}"'
    where = " ".join(x for x in (f'"{county} County"' if county else "", STATE_NAMES.get(state, state)) if x)
    if state == "MA":
        phrases = ['"zoning bylaw"', '"zoning bylaw" solar', '"solar overlay"', '"battery energy storage"', "moratorium", '"zoning ordinance"']
    else:
        phrases = ['"zoning law"', '"zoning ordinance"', '"solar" "local law"', '"battery energy storage"', "moratorium"]
        y = year or date.today().year
        phrases += [f'"Local Law" {y}', f'"Local Law" {y - 1}']
    return [f"{t} {p} {where}" for p in phrases]


def host_guesses(state: str, town: str) -> list[str]:
    s = slug(town)
    if state == "MA":
        return [f"www.{s}-ma.gov", f"www.{s}ma.gov", f"www.{s}ma.org", f"www.{s}.ma.us"]
    if state == "NY":
        return [f"www.townof{s}ny.gov", f"www.{s}.gov", f"www.{s}ny.gov", f"www.townof{s}.org"]
    return [f"www.{s}{state.lower()}.gov", f"www.{s}.gov"]


TOWN_PATHS = ["", "/planning", "/planning-board", "/zoning", "/bylaws", "/zoning-bylaw", "/DocumentCenter"]


def code_host_urls(state: str, town: str) -> list[str]:
    st, s = state.lower(), slug(town)
    return [f"https://library.municode.com/{st}/{s}",
            f"https://ecode360.com/search?query={quote_plus(f'{town} {STATE_NAMES.get(state, state)}')}",
            f"https://www.generalcode.com/source-library/?state={state.upper()}"]


def known_urls(state: str, town: str) -> list[str]:
    """Primary-source URLs already in the tracker for this town (never news or state pages)."""
    rec = tracker.load().get(tracker.key(state, town))
    if not rec:
        return []
    return [s["url"] for s in rec.sources if s.get("url") and "primary" in str(s.get("kind", "")) and not s["url"].startswith("data/")]


def norm_url(url: str) -> str:
    p = urlsplit(url.strip())
    path = re.sub(r"/+$", "", p.path) or "/"
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), path, p.query, ""))


# ------------------------------------------------------------------------------------------- fetching (cached, polite)
class Fetched(BaseModel):
    url: str
    status: int | None
    content_type: str = ""
    body: bytes = b""
    error: str = ""
    retrieved: str = ""


_last_live = 0.0


def fetch(url: str, refresh: bool = False) -> Fetched:
    """GET with a disk cache keyed by URL hash. Errors (403, 404, DNS) are cached too, so a rerun doesn't
    hammer a site that refused us; pass refresh=True to try again."""
    global _last_live
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    d = cache_dir()
    meta_p, body_p = d / f"{key}.json", d / f"{key}.bin"
    if not refresh and meta_p.exists():
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        return Fetched(**meta, body=body_p.read_bytes() if body_p.exists() else b"")
    wait = DELAY_S - (time.monotonic() - _last_live)
    if wait > 0:
        time.sleep(wait)
    _last_live = time.monotonic()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        r = requests.get(url, timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
        f = Fetched(url=url, status=r.status_code, content_type=r.headers.get("content-type", ""),
                    body=r.content if r.ok else b"", retrieved=now)
    except requests.RequestException as e:
        msg = "host not found" if "NameResolution" in str(e) or "getaddrinfo" in str(e) else f"{type(e).__name__}: {str(e)[:120]}"
        f = Fetched(url=url, status=None, error=msg, retrieved=now)
    d.mkdir(parents=True, exist_ok=True)
    meta_p.write_text(json.dumps(f.model_dump(exclude={"body"})), encoding="utf-8")
    body_p.write_bytes(f.body)
    return f


def web_search(query: str, count: int = 8) -> list[str]:
    """Brave Search API, only when BRAVE_SEARCH_API_KEY is set. Never scrapes a search engine's HTML."""
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        return []
    r = requests.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": count},
                     headers={"X-Subscription-Token": key, "Accept": "application/json", "User-Agent": USER_AGENT},
                     timeout=TIMEOUT_S)
    r.raise_for_status()
    return [x["url"] for x in r.json().get("web", {}).get("results", []) if x.get("url")]


HEAD_PAGES = 3          # classification only needs the title and opening text
HEAD_CHARS = 6000


def to_text(f: Fetched) -> tuple[str, str, bool]:
    """(opening text, title, scanned). HTML and text go through extract.load_source, so discovery reads them the
    way extraction will. PDFs read only their first HEAD_PAGES pages: a 140-page code took seconds per document,
    and fewer than 100 characters of text on those pages means scanned. Results are cached by content hash."""
    key = hashlib.sha1(f.body).hexdigest()[:20]
    cached = cache_dir() / f"{key}.head.json"
    if cached.exists():
        d = json.loads(cached.read_text(encoding="utf-8"))
        return d["text"], d["title"], d["scanned"]
    if f.body[:4] == b"%PDF":
        text, title, scanned = _pdf_head(f.body)
    else:
        ext = ".html" if ("html" in f.content_type.lower() or f.body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))) else ".txt"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"doc{ext}"
            p.write_bytes(f.body)
            text, _ = extract.load_source(str(p))
        title, scanned = (_html_title(f.body) if ext == ".html" else ""), False
    text = text[:HEAD_CHARS]
    if not title and not scanned:
        title = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 8 and not ln.startswith("[page")), "")[:160]
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps({"text": text, "title": title, "scanned": scanned}), encoding="utf-8")
    return text, title, scanned


def _pdf_head(body: bytes) -> tuple[str, str, bool]:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    pages = reader.pages[:HEAD_PAGES]
    text = "\n\n".join(f"[page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(pages))
    body_chars = len(re.sub(r"\[page \d+\]|\s", "", text))
    return text, _pdf_title(body), body_chars < 100


def _html_title(body: bytes) -> str:
    from bs4 import BeautifulSoup

    t = BeautifulSoup(body, "html.parser").title
    return re.sub(r"\s+", " ", t.get_text()).strip()[:200] if t else ""


def _pdf_title(body: bytes) -> str:
    try:
        import io

        from pypdf import PdfReader

        meta = PdfReader(io.BytesIO(body)).metadata
        return str(meta.title or "")[:200] if meta else ""
    except Exception:
        return ""


LINK_WORDS = re.compile(r"zoning|by-?law|ordinance|solar|battery|energy storage|\bbess\b|moratori|local law|code of", re.I)


def follow_links(f: Fetched, town: str, limit: int = 15) -> list[str]:
    """One hop from a town or code-host page: links whose text or href looks like a bylaw, law or code.
    On code-host index pages the anchor must also name the town."""
    from bs4 import BeautifulSoup

    if not f.body or "html" not in f.content_type.lower():
        return []
    host = urlsplit(f.url).netloc.lower()
    on_code_host = any(host.endswith(h) for h in CODE_HOSTS)
    t = norm_town(town)
    out = []
    for a in BeautifulSoup(f.body, "html.parser").find_all("a", href=True):
        text, href = a.get_text(" ", strip=True), a["href"]
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        hit = t in text.casefold() if on_code_host else bool(LINK_WORDS.search(f"{text} {href}"))
        if not hit:
            continue
        u = urljoin(f.url, href)
        if u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------------------------------- classification
_MUNI = re.compile(r"\b(?:town|city|village|township)\s+of\s+([a-z][a-z.'\-]+(?:\s+[a-z][a-z.'\-]+)?)", re.I)
_STOP = {"the", "and", "board", "planning", "zoning", "code", "law", "local", "new", "york", "massachusetts"}


def named_municipalities(text: str) -> set[str]:
    out = set()
    for m in _MUNI.finditer(text):
        words = [w for w in m.group(1).casefold().split() if w not in _STOP]
        if words:   # "Town of Red Hook" -> {"red hook", "red"}; "Town of Sutton Conservation" -> {..., "sutton"}
            out.update({" ".join(words[:2]), words[0]})
    return out


def municipality_match(town: str, title: str, head: str, url: str) -> Match:
    """Title decides first (a document's own name is the strongest signal), then the first ~2k characters,
    then the URL. Naming a different municipality, or a known confusable, is a mismatch."""
    t = norm_town(town)
    word = re.compile(rf"(?<![a-z]){re.escape(t)}(?![a-z])", re.I)
    confusables = CONFUSABLE.get(t, [])

    def judge(text: str) -> Match | None:
        if not text:
            return None
        named = named_municipalities(text)
        if t in named:
            return "exact"
        if named:   # names some other municipality: only "likely" if it also names ours
            return "likely" if word.search(text) else "mismatch"
        if word.search(text):
            return "likely"
        if any(re.search(rf"(?<![a-z]){re.escape(c)}(?![a-z])", text, re.I) for c in confusables):
            return "mismatch"
        return None

    for part in (title, head[:2000]):
        verdict = judge(part)
        if verdict:
            return verdict
    host = urlsplit(url).netloc.lower()
    if slug(town) and slug(town) in host.replace("-", "") and not any(c in host for c in confusables if c != t and len(c) > len(t)):
        return "likely"
    return "unknown"


_PROPOSED = re.compile(r"\bproposed\b|\bdraft\b|for discussion|notice of public hearing|public hearing notice|draft local law", re.I)
_MODEL = re.compile(r"\bmodel\s+(?:solar\s+|bess\s+|battery\s+[a-z ]*?|zoning\s+)?(?:by-?law|law|ordinance)s?\b", re.I)
_LOCAL_LAW = re.compile(r"local\s+law\s+(?:no\.?|number|#)?\s*\d+\s*(?:of|-|–)\s*(?:19|20)\d{2}", re.I)
_STAFF = re.compile(r"staff report|memorandum|\bminutes\b|\bagenda\b", re.I)
_INDEX = re.compile(r"/search|source-library|[?&](q|query|search)=", re.I)
_LOGIN = re.compile(r"log ?in|sign ?in", re.I)
_TOPIC = re.compile(r"zoning|solar|\bbess\b|battery|energy storage|moratori|by-?law", re.I)


def classify(c: Candidate, state: str, town: str, text: str) -> Candidate:
    c = c.model_copy()
    host = urlsplit(c.url).netloc.lower()
    head = text[:2000]
    years = [int(y) for y in re.findall(r"\b(19[89]\d|20\d{2})\b", f"{c.title} {c.url}")]
    c.year = max(years) if years else None
    c.excerpt = re.sub(r"\s+", " ", f"{c.title} | {head[:300]}").strip(" |")[:300]
    if c.http_status is None or c.http_status >= 400:
        c.reject_reason = c.reject_reason or (f"HTTP {c.http_status}" if c.http_status else "fetch failed")
        return c
    if c.scanned:
        c.reject_reason = "scanned PDF (no text layer); OCR it before extracting"
        c.kind = "clerk_pdf"
        return c
    if not text.strip():
        c.reject_reason = "empty body"
        return c
    if _INDEX.search(urlsplit(c.url).path + "?" + urlsplit(c.url).query) or _LOGIN.search(c.title):
        c.reject_reason = "search, index or login page (its links were followed); not a document"
        return c
    c.municipality_match = municipality_match(town, c.title, head, c.url)
    is_pdf = "pdf" in c.content_type.lower() or c.url.lower().endswith(".pdf")
    if _MODEL.search(f"{c.title} {head}") and (any(host.endswith(h) for h in MODEL_ISSUERS) or not c.municipality_match == "exact"):
        c.kind = "model_bylaw"
    elif any(host.endswith(h) for h in NEWS_HOSTS) or "/news/" in c.url.lower():
        c.kind = "news"
    elif _PROPOSED.search(f"{c.title} {head[:600]}"):
        c.kind = "proposed"
    elif any(host.endswith(h) for h in CODE_HOSTS):
        c.kind = "living_code"
    elif _LOCAL_LAW.search(f"{c.title} {head[:600]}"):
        c.kind = "local_law"
    elif _STAFF.search(c.title):
        c.kind = "staff_report"
    elif is_pdf:
        c.kind = "clerk_pdf"
    else:
        c.kind = "unknown"
    if c.municipality_match == "mismatch":
        c.reject_reason = "names a different municipality"
    elif c.kind == "model_bylaw":
        c.reject_reason = "state model bylaw / template, not the town's law"
    elif c.kind == "proposed":
        c.reject_reason = "proposed, draft or hearing notice; not adopted law"
    return c


def score(c: Candidate, this_year: int | None = None) -> float:
    """Ranking tiers: living code on a code host > town PDF with a recent year > dated local law > other town PDF >
    other town HTML > staff report > news. Tiers are 100 apart and the in-tier bonuses (topic words in the title,
    exact town match) add at most 30, so a bonus reorders documents within a tier but never across tiers."""
    y = this_year or date.today().year
    recent = bool(c.year and c.year >= y - 3)
    tier = {"living_code": 6, "local_law": 4, "unknown": 2, "staff_report": 1, "news": 0}.get(c.kind, 0)
    if c.kind == "clerk_pdf":
        tier = 5 if recent else 3
    s = 100 * tier
    if _TOPIC.search(c.title):
        s += 20
    s += {"exact": 10, "likely": 0, "unknown": -10}.get(c.municipality_match, 0)
    return float(max(s, 100 * tier - 10))


def rank(cands: list[Candidate]) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    keep = [c for c in cands if not c.reject_reason]
    rejects = [c for c in cands if c.reject_reason]
    for c in keep:
        c.score = score(c)
    keep.sort(key=lambda c: (-c.score, c.url))
    for i, c in enumerate(keep, start=1):
        c.rank = i
    return keep[:MAX_SHOWN], keep[MAX_SHOWN:], rejects


# ------------------------------------------------------------------------------------------- orchestration
def queue_path(state: str, town: str) -> Path:
    return queue_dir() / f"{state.upper()}_{re.sub(r'[^A-Za-z0-9]+', '_', town.strip().title())}.json"


def _candidate(url: str, source: str, f: Fetched) -> Candidate:
    c = Candidate(url=url, source=source, http_status=f.status, content_type=f.content_type, retrieved=f.retrieved)
    if f.error:
        c.reject_reason = f.error
    return c


def discover(state: str, town: str, county: str = "", seed_urls: list[str] | None = None, refresh: bool = False,
             progress=print) -> DiscoverResult:
    state = state.upper()
    queries = build_queries(state, town, county)
    planned: list[tuple[str, str]] = [(u, "seed") for u in (seed_urls or [])]
    planned += [(u, "tracker") for u in known_urls(state, town)]
    planned += [(u, "pattern") for u in code_host_urls(state, town)]
    for h in host_guesses(state, town):
        planned.append((f"https://{h}/", "pattern"))
    searched = bool(os.environ.get("BRAVE_SEARCH_API_KEY"))
    if searched:
        sites = " OR ".join(f"site:{h}" for h in ("ecode360.com", "library.municode.com", "generalcode.com"))
        for q in queries:
            try:
                planned += [(u, "search") for u in web_search(f"{q} ({sites} OR site:.gov OR site:.us)")]
            except Exception as e:
                progress(f"search failed for {q!r}: {type(e).__name__}")

    seen, bodies, cands, fetches = set(), set(), [], 0
    queue = [(u, src, 0) for u, src in planned]   # (url, source, link depth)
    while queue and fetches < MAX_FETCHES:
        url, source, depth = queue.pop(0)
        k = norm_url(url)
        if k in seen:
            continue
        seen.add(k)
        f = fetch(url, refresh=refresh)
        fetches += 1
        c = _candidate(url, source, f)
        digest = hashlib.sha1(f.body).hexdigest() if f.body else None
        if digest and digest in bodies:      # same document served from another host or path
            continue
        if digest:
            bodies.add(digest)
        text = ""
        if not c.reject_reason and f.status and f.status < 400 and f.body:
            try:
                text, c.title, c.scanned = to_text(f)
            except Exception as e:           # corrupt PDF, odd encoding: record it, keep going
                c.reject_reason = f"unreadable document ({type(e).__name__})"
        cands.append(classify(c, state, town, text))
        host = urlsplit(url).netloc.lower()
        is_guess_root = source == "pattern" and urlsplit(url).path in ("", "/") and not any(host.endswith(h) for h in CODE_HOSTS)
        if is_guess_root and f.status == 200:      # a guessed town host answered: try its usual paths
            queue += [(f"https://{host}{p}", "pattern", 0) for p in TOWN_PATHS[1:]]
        # Two hops at most: index page -> "Charter & By-Laws" page -> the bylaw PDF. The second hop stays on the
        # same host, so a town page can't walk the run off into the wider web.
        if f.status == 200 and depth < 2 and (source != "search"):
            links = follow_links(f, town)
            if depth == 1:
                links = [u for u in links if urlsplit(u).netloc.lower() == host]
            queue += [(u, "link", depth + 1) for u in links]

    shown, more, rejects = rank(cands)
    prev = queue_path(state, town)
    old, prev_run = set(), None
    if prev.exists():
        p = json.loads(prev.read_text(encoding="utf-8"))
        prev_run = p.get("retrieved")
        old = {norm_url(c["url"]) for part in ("candidates", "more", "rejects") for c in p.get(part, [])}
    res = DiscoverResult(state=state, municipality=town, county=county, queries=queries, searched_web=searched,
                         retrieved=datetime.now().isoformat(timespec="seconds"), candidates=shown, more=more, rejects=rejects,
                         previous_run=prev_run, new_urls=[c.url for c in cands if norm_url(c.url) not in old] if prev_run else [])
    prev.parent.mkdir(parents=True, exist_ok=True)
    prev.write_text(res.model_dump_json(indent=2), encoding="utf-8")
    return res


def load_queue(state: str, town: str) -> DiscoverResult:
    p = queue_path(state, town)
    if not p.exists():
        raise FileNotFoundError(f"No discovery queue for {town}, {state}. Run `reservoir bylaws discover` first.")
    return DiscoverResult.model_validate_json(p.read_text(encoding="utf-8"))


def pick(state: str, town: str, n: int) -> Candidate:
    """Ranked candidate #n from the saved queue. News and rejects can't be picked for extraction."""
    q = load_queue(state, town)
    ranked = q.candidates + q.more
    match = [c for c in ranked if c.rank == n]
    if not match:
        raise ValueError(f"No ranked candidate #{n} for {town}, {state} (queue has {len(ranked)}).")
    c = match[0]
    if not c.extractable:
        raise ValueError(f"Candidate #{n} is {c.kind}{': ' + c.reject_reason if c.reject_reason else ''}; pick a primary document.")
    return c


def summary(res: DiscoverResult) -> dict:
    """Compact JSON for the CLI."""
    row = lambda c: {k: v for k, v in {"rank": c.rank, "url": c.url, "kind": c.kind, "match": c.municipality_match,
                                        "title": c.title[:90], "status": c.http_status, "reject_reason": c.reject_reason}.items() if v not in (None, "")}
    return {"town": f"{res.municipality}, {res.state}", "searched_web": res.searched_web, "queue": str(queue_path(res.state, res.municipality)),
            "candidates": [row(c) for c in res.candidates], "more": len(res.more),
            "rejects": [row(c) for c in res.rejects], "new_urls": res.new_urls,
            "next": f"reservoir bylaws extract --state {res.state} --town \"{res.municipality}\" --from-discover 1"}
