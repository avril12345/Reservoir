"""Record a narrated walkthrough of the app as an MP4 (for portfolios / job applications).

    uv run --with playwright --with imageio-ffmpeg python tools/demo_video.py [--voice "Microsoft Zira Desktop"]

What it does
  1. Builds the narration from the latest run outputs (numbers and site names are read, not typed).
  2. Speaks each segment with the built-in Windows voice (System.Speech): no cloud service, no key.
  3. Starts the Streamlit app with RESERVOIR_DEMO_REDACT=1 (individual / trust owners masked).
  4. Drives Microsoft Edge through every tab with Playwright, showing captions, and records the screen.
  5. Muxes video + narration into outputs/video/reservoir_walkthrough.mp4, and writes the script and
     an .srt subtitle file next to it so you can re-record the voice-over yourself.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
REGION = "ma_ngrid_blackstone"
OUT = ROOT / "outputs" / "video"
W, H = 1600, 900
PORT = 8502


# ------------------------------------------------------------------------------------------- script
@dataclass
class Segment:
    chapter: str
    say: str                                  # spoken text (written for the voice: no unit abbreviations)
    actions: list = field(default_factory=list)   # [(seconds_into_segment, fn(page))]
    captions: list[str] | None = None         # on-screen text; defaults to the spoken sentences
    wav: Path | None = None
    dur: float = 0.0
    start: float = 0.0


def facts() -> dict:
    d = ROOT / "outputs" / REGION
    meta = json.loads((d / "run_meta.json").read_text())
    s = pd.read_csv(d / "shortlist.csv")
    top = s.iloc[0]
    lf = s[(s.site_type == "landfill") & (s.dist_3ph_m > 300)]
    far = (lf if len(lf) else s[s.site_type == "landfill"]).iloc[0]
    return {"meta": meta, "top": top, "far": far}


def town(t) -> str:
    return str(t).title()


def build_segments(f: dict) -> list[Segment]:
    m, top, far = f["meta"], f["top"], f["far"]
    top_kind = top.site_type.replace("_", " ")
    if top.site_type == "landfill" and isinstance(top.landfill_label, str) and top.landfill_label:
        name, _, status = top.landfill_label.rpartition(" ")
        top_said = f"the {name.title()}, listed by MassDEP as {status.lower()}"
        top_cap = f"{name.title()} ({status.lower()})"
    else:
        top_said = top_cap = f"a {town(top._town)} {top_kind} site"
    return [
        Segment("Reservoir",
                "This is Reservoir, an open-source origination tool for distributed solar and storage. "
                "It screens every parcel in a utility territory for good sites, and it tracks the local permitting rules "
                "that kill most small projects.",
                actions=[(0, lambda p: title_card(p, "Reservoir",
                                                  "DG solar + storage site screener · local siting & moratorium tracker"))],
                captions=["Reservoir: an open-source origination tool for distributed solar + storage.",
                          "Screens every parcel in a utility territory, and tracks the local permitting rules that kill small projects."]),
        Segment("Pilot region",
                f"This pilot covers four National Grid towns in Massachusetts' Blackstone Valley. "
                f"The pipeline pulled {m['lots']:,} tax lots from MassGIS, merged same-owner neighbours into "
                f"{m['sites_analysed']:,} candidate sites of three acres or more, and {m['passing_filters']} of them pass the hard filters.",
                actions=[(0, lambda p: title_card(p, None)), (0.2, lambda p: tab(p, "Map"))]),
        Segment("Map · constraints and grid",
                "Every input is public. Wetlands, FEMA flood zones, protected open space, slope from USGS elevation data, "
                "land cover and building footprints are cut out to leave the buildable area, shown in orange. "
                "The dots are three-phase feeders from National Grid's hosting capacity map, coloured by remaining headroom: "
                "green has room, red has none.",
                actions=[(2, lambda p: map_dblclick(p, 0.49, 0.70)), (4, lambda p: map_dblclick(p, 0.5, 0.5)), (19, lambda p: map_zoom(p, -2))],
                captions=["Wetlands (NWI + MassDEP), FEMA flood zones, protected open space, 3DEP slope, NLCD land cover and buildings are removed → buildable area (orange).",
                          "Dots = 3-phase feeders from National Grid's hosting-capacity map, coloured by headroom: green has room, red has none."]),
        Segment("Shortlist · ranked sites",
                f"Scoring follows the East Light pattern and the SMART 3.0 priority for landfills and brownfields. "
                f"Number one is {top_said}, on a feeder with {top.hc_mw:.1f} megawatts of headroom. "
                "Seven components make up each score, and every one of them is visible.",
                actions=[(0.2, lambda p: tab(p, "Shortlist")), (7, lambda p: scroll_main(p, 250)), (12, lambda p: scroll_main(p, -250))],
                captions=["Scoring favours landfills, brownfields, gravel pits and industrial land (East Light pattern, SMART 3.0 priority).",
                          f"#1: {top_cap} on feeder {top.feeder_id}, {top.hc_mw:.1f} MW headroom.",
                          "Seven score components, all visible."]),
        Segment("Live re-weighting",
                "Weights are live. Raising the weight on feeder headroom pushes sites on saturated feeders down the list "
                "straight away, without re-running any geometry.",
                actions=[(0.3, lambda p: open_weights(p)), (2.5, lambda p: nudge_slider(p, "grid hosting", 15)),
                         (9, lambda p: nudge_slider(p, "grid hosting", -15))]),
        Segment("Site cards",
                f"Each site gets a one-page card: map, acreage, buildable area, the nearest three-phase feeder and its "
                f"hosting capacity, the property-line setback used, permitting status, and flags. "
                f"This {town(far._town)} landfill is about {far.dist_3ph_m:,.0f} metres from three-phase power, and the card draws that path. "
                "Cards print straight to PDF.",
                actions=[(0.2, lambda p: close_weights(p)), (0.6, lambda p: tab(p, "Site cards")),
                         (1.5, lambda p: pick(p, "Parcel", f"#{int(far['rank'])} ·")),
                         (9, lambda p: scroll_card(p, 450)), (17, lambda p: scroll_card(p, 500))],
                captions=["One-page card per site: map, acreage, buildable area, nearest 3-phase feeder + hosting capacity, setback, permitting, flags.",
                          f"{town(far._town)} landfill: ~{far.dist_3ph_m:,.0f} m to 3-phase, drawn on the map.",
                          "Cards print to PDF."]),
        Segment("Owner outreach",
                "The owner list groups sites by owner and mailing address, tags public, business and absentee owners, "
                "and exports ready for a mail merge. Private individuals are masked for this recording.",
                actions=[(0.3, lambda p: tab(p, "Owner outreach"))]),
        Segment("Siting & moratorium tracker",
                "The second tool is the local siting and moratorium tracker. Claude reads a zoning bylaw or local law and "
                "extracts structured fields: by right or special permit, size caps, setbacks, decommissioning, and moratorium dates. "
                "Every field has to carry a verbatim quote, and code checks that quote against the source. "
                "Anything unsupported is thrown out.",
                actions=[(0.3, lambda p: tab(p, "Siting")), (8, lambda p: scroll_main(p, 350))]),
        Segment("Tracker · Cortlandt vs Cortlandville",
                "Here, the Town of Cortlandt's local law extends its battery storage moratorium through December 31st, 2026. "
                "It is kept apart from Cortlandville, a different town that search results often mix up with it. "
                "Cortlandville's Tier 2 battery ban is recorded as proposed, because its adoption isn't confirmed. "
                "For Massachusetts towns, the tracker also records the consolidated local permit, mandatory from October 1st, 2026. "
                "Permitting status flows back into each site's score, and a town's solar setback replaces the default in the buildable-area geometry.",
                actions=[(0.2, lambda p: pick(p, "Municipality", "Cortlandt")), (1.2, lambda p: scroll_main(p, 300))],
                captions=["Cortlandt (Westchester) LL 1-2026: BESS moratorium extended through Dec 31, 2026, quote verified against the law.",
                          "Kept separate from Cortlandville (Cortland Co.), whose Tier 2 BESS ban is recorded as proposed.",
                          "MA towns: consolidated local permit (225 CMR 29.00), mandatory from Oct 1, 2026.",
                          "Permitting status feeds each site's score; a town's solar setback replaces the default in the geometry."]),
        Segment("Accuracy check",
                "Language models fail in predictable ways: invented values, omissions, stale laws, confused towns. "
                "This tab scores the tracker against twenty hand-checked towns, with confidence intervals and an error taxonomy. "
                "It stays at zero until a person has done the checking, rather than showing a made-up number.",
                actions=[(0.3, lambda p: tab(p, "Accuracy")), (8, lambda p: scroll_main(p, 500))]),
        Segment("Method & sources",
                "Everything is reproducible. One command fetches and caches the data, a test suite checks the pipeline "
                "against hand-worked answers, and the method tab lists every source and limitation. "
                "New York and Oregon configurations come next.",
                actions=[(0.3, lambda p: tab(p, "Method")), (6, lambda p: scroll_main(p, 500))]),
        Segment("Thanks",
                "Thanks for watching.",
                actions=[(0.2, lambda p: title_card(p, "Reservoir", "uv run reservoir run  ·  uv run streamlit run app/streamlit_app.py"))],
                captions=[""]),
    ]


def short_segments(f: dict) -> list[Segment]:
    """The two-minute cut: one idea per scene, plain spoken English, numbers read from the latest run."""
    m, top, far = f["meta"], f["top"], f["far"]
    if top.site_type == "landfill" and isinstance(top.landfill_label, str) and top.landfill_label:
        name, _, status = top.landfill_label.rpartition(" ")
        top_said = f"the {status.lower()} {name.title()}"
    else:
        top_said = f"a {top.site_type.replace('_', ' ')} site in {town(top._town)}"
    return [
        Segment("Reservoir",
                "This is Reservoir. It finds sites for small solar and storage projects, "
                "and tracks the local rules that can stop them.",
                actions=[(0, lambda p: title_card(p, "Reservoir", "DG solar + storage site screener · local siting & moratorium tracker"))],
                captions=["Reservoir finds sites for small solar + storage projects,",
                          "and tracks the local rules that can stop them."]),
        Segment("Screening the territory",
                "It's screening four towns in Massachusetts. Wetlands, flood zones and steep ground are cut away, "
                "leaving the buildable land in orange. Green feeders have room on the grid; red ones don't.",
                actions=[(0, lambda p: title_card(p, None)), (0.2, lambda p: tab(p, "Map")),
                         (3, lambda p: map_dblclick(p, 0.49, 0.70)), (5, lambda p: map_dblclick(p, 0.5, 0.5))],
                captions=["Four National Grid towns in Massachusetts' Blackstone Valley.",
                          "Wetlands, FEMA flood zones, steep slopes and protected land are removed → buildable area (orange).",
                          "Dots = 3-phase feeders from National Grid's hosting-capacity map: green has headroom, red has none."]),
        Segment("Ranked shortlist",
                f"Landfills, brownfields and industrial land rank highest. Top of the list is {top_said}, "
                f"with {top.hc_mw:.1f} megawatts of headroom.",
                actions=[(0.2, lambda p: map_zoom(p, -2)), (0.6, lambda p: tab(p, "Shortlist"))],
                captions=[f"{m['sites_analysed']:,} candidate sites → {m['passing_filters']} pass. Landfills, brownfields, pits and industrial land rank highest.",
                          f"#1: {top_said.removeprefix('the ').title()}, {top.hc_mw:.1f} MW feeder headroom."]),
        Segment("Live weights",
                "Weights are live, so favouring grid headroom reorders the list instantly.",
                actions=[(0.2, lambda p: open_weights(p)), (1.2, lambda p: nudge_slider(p, "grid hosting", 15))]),
        Segment("Site cards",
                f"Every site gets a one-page card. This {town(far._town)} landfill is about "
                f"{far.dist_3ph_m:,.0f} metres from three-phase power.",
                actions=[(0.1, lambda p: nudge_slider(p, "grid hosting", -15)), (0.6, lambda p: close_weights(p)),
                         (1.0, lambda p: tab(p, "Site cards")), (1.6, lambda p: pick(p, "Parcel", f"#{int(far['rank'])} ·")),
                         (9, lambda p: scroll_card(p, 450))],
                captions=["One-page card per site: map, buildable acres, nearest 3-phase feeder + capacity, setbacks, flags.",
                          f"{town(far._town)} landfill: ~{far.dist_3ph_m:,.0f} m to 3-phase, drawn on the map."]),
        Segment("Owner outreach",
                "Owners are grouped for outreach, with private names hidden here.",
                actions=[(0.2, lambda p: tab(p, "Owner outreach"))]),
        Segment("Finding the bylaw",
                "The second tool tracks local bylaws. It finds likely source documents, sets aside drafts and the "
                "wrong town, and a person picks the right one.",
                actions=[(0.2, lambda p: tab(p, "Siting")), (0.8, lambda p: run_discovery(p, "NY", "Cortlandt", "Westchester"))],
                captions=["Tool 2: the local siting & moratorium tracker.",
                          "Discovery ranks likely source documents and sets aside drafts, dead links and the wrong town. A person picks."]),
        Segment("Cortlandt, not Cortlandville",
                "Every extracted value quotes its source. Cortlandt's battery moratorium runs to the end of 2026, "
                "kept apart from Cortlandville, a different town.",
                captions=["Extraction quotes the source text, and code checks every quote.",
                          "Cortlandt (Westchester): BESS moratorium through Dec 31, 2026. Kept apart from Cortlandville (Cortland Co.)."]),
        Segment("Accuracy check",
                "A twenty-town hand check measures accuracy, and shows nothing until it's done.",
                actions=[(0.2, lambda p: tab(p, "Accuracy"))]),
        Segment("Thanks",
                "Thanks for watching.",
                actions=[(0.2, lambda p: title_card(p, "Reservoir", "uv run reservoir run  ·  uv run streamlit run app/streamlit_app.py"))],
                captions=["Open source and reproducible."]),
    ]


# ------------------------------------------------------------------------------------------- browser helpers
OVERLAY_JS = """
({cap, chap}) => {
  const mk = (id, css) => { let e = document.getElementById(id); if (!e) { e = document.createElement('div'); e.id = id;
      Object.assign(e.style, css); document.body.appendChild(e); } return e; };
  const c = mk('demo-cap', {position:'fixed', left:'50%', bottom:'26px', transform:'translateX(-50%)', maxWidth:'1180px',
      background:'rgba(17,22,30,.88)', color:'#fff', font:'600 19px/1.4 "Segoe UI",system-ui,sans-serif',
      padding:'10px 20px', borderRadius:'10px', zIndex:2147483647, textAlign:'center', pointerEvents:'none'});
  c.textContent = cap; c.style.display = cap ? 'block' : 'none';
  const h = mk('demo-chap', {position:'fixed', right:'22px', top:'64px', background:'#b9770e', color:'#fff',
      font:'700 14px/1 "Segoe UI",system-ui,sans-serif', letterSpacing:'.05em', textTransform:'uppercase',
      padding:'8px 12px', borderRadius:'6px', zIndex:2147483647, pointerEvents:'none'});
  h.textContent = chap; h.style.display = chap ? 'block' : 'none';
}
"""

TITLE_JS = """
({title, sub}) => {
  let e = document.getElementById('demo-title');
  if (!title) { if (e) e.remove(); return; }
  if (!e) { e = document.createElement('div'); e.id = 'demo-title'; document.body.appendChild(e); }
  Object.assign(e.style, {position:'fixed', inset:'0', zIndex:2147483646, display:'flex', flexDirection:'column',
      alignItems:'center', justifyContent:'center', background:'linear-gradient(135deg,#17202a,#1b4f72)', color:'#fff',
      font:'"Segoe UI",system-ui,sans-serif'});
  e.innerHTML = `<div style="font-size:76px;font-weight:800;letter-spacing:-.02em">☀️ ${title}</div>
                 <div style="font-size:26px;opacity:.85;margin-top:14px">${sub}</div>`;
}
"""

HIDE_CHROME_CSS = """
[data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"], .stDeployButton { display: none !important; }
"""


def title_card(p, title, sub=""):
    p.evaluate(TITLE_JS, {"title": title, "sub": sub})


def tab(p, name):
    p.get_by_role("tab").filter(has_text=name).first.click()
    settle(p)


def settle(p, timeout=15_000):
    """Wait for Streamlit to finish rerunning."""
    p.wait_for_timeout(300)
    try:
        p.wait_for_function("() => { const a = document.querySelector('[data-testid=\"stApp\"]');"
                            " return !a || a.getAttribute('data-test-script-state') !== 'running'; }", timeout=timeout)
    except Exception:
        pass


def scroll_main(p, dy):
    p.mouse.move(W * 0.6, H * 0.55)
    p.mouse.wheel(0, dy)


def scroll_card(p, dy):
    frames = [f for f in p.locator("iframe").all() if (f.bounding_box() or {}).get("height", 0) > 600]
    if not frames:
        return scroll_main(p, dy)
    b = frames[-1].bounding_box()
    p.mouse.move(b["x"] + b["width"] / 2, b["y"] + min(b["height"] / 2, 400))
    p.mouse.wheel(0, dy)


def _map_box(p):
    return p.locator("iframe").first.bounding_box()


def map_dblclick(p, fx, fy):
    """Zoom in one level centred on a point given as a fraction of the map frame."""
    b = _map_box(p)
    p.mouse.dblclick(b["x"] + b["width"] * fx, b["y"] + b["height"] * fy)
    p.wait_for_timeout(700)


def map_zoom(p, n):
    fr = p.frame_locator("iframe").first
    sel = ".leaflet-control-zoom-in" if n > 0 else ".leaflet-control-zoom-out"
    for _ in range(abs(n)):
        try:
            fr.locator(sel).first.click(timeout=5000)
        except Exception:
            break
        p.wait_for_timeout(500)


def open_weights(p):
    p.get_by_text("Score weights (live re-rank)").first.click()
    p.wait_for_timeout(400)


def close_weights(p):
    try:
        p.get_by_text("Score weights (live re-rank)").first.click(timeout=3000)
    except Exception:
        pass


def nudge_slider(p, label, steps):
    s = p.locator('[data-testid="stSlider"]').filter(has_text=label).get_by_role("slider").first
    s.focus()
    key = "ArrowRight" if steps > 0 else "ArrowLeft"
    for _ in range(abs(steps)):
        p.keyboard.press(key)
        p.wait_for_timeout(60)
    settle(p)


def run_discovery(p, state, town_name, county):
    """Open 'Find likely sources' on the tracker tab and search (served from the discovery cache when warm)."""
    p.get_by_text("Find likely sources").first.click()
    p.wait_for_timeout(500)
    exp = p.locator('[data-testid="stExpander"]').filter(has_text="Find likely sources").first
    exp.locator('[data-testid="stSelectbox"]').filter(has_text="State").first.click()
    p.get_by_role("option", name=state, exact=True).first.click()
    exp.locator('[data-testid="stTextInput"]').filter(has_text="Municipality").locator("input").first.fill(town_name)
    exp.locator('[data-testid="stTextInput"]').filter(has_text="County").locator("input").first.fill(county)
    exp.get_by_role("button", name="Search").click()
    settle(p, 90_000)
    exp.scroll_into_view_if_needed()
    p.wait_for_timeout(300)
    scroll_main(p, 120)


def pick(p, label, text):
    box = p.locator('[data-testid="stSelectbox"]').filter(has_text=label).first
    box.scroll_into_view_if_needed()
    box.click()
    p.wait_for_timeout(300)
    p.get_by_role("option").filter(has_text=text).first.click()
    settle(p)


# ------------------------------------------------------------------------------------------- audio
def speak(text: str, path: Path, voice: str, rate: int):
    txt = path.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    ps = (f"Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SelectVoice('{voice}'); $s.Rate = {rate}; $s.SetOutputToWaveFile('{path}'); "
          f"$s.Speak([IO.File]::ReadAllText('{txt}')); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def split_captions(seg: Segment) -> list[tuple[float, str]]:
    """(offset_seconds, text) for each caption, timed by word count across the segment's narration."""
    import re

    caps = seg.captions or [s.strip() for s in re.split(r"(?<=[.!?])\s+", seg.say) if s.strip()]
    words = [max(len(c.split()), 1) for c in caps]
    tot, t, out = sum(words), 0.0, []
    for c, n in zip(caps, words):
        out.append((t, c))
        t += seg.dur * n / tot
    return out


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


# ------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cut", choices=["short", "full"], default="short", help="short ~2 min, full ~4 min")
    ap.add_argument("--voice", default="Microsoft Hazel Desktop", help="installed System.Speech voice (Hazel = British English)")
    ap.add_argument("--rate", type=int, default=-1, help="System.Speech rate, -10..10 (negative = slower, more relaxed)")
    ap.add_argument("--pad", type=float, default=0.6, help="silence after each segment (s)")
    a = ap.parse_args()

    import imageio_ffmpeg
    from playwright.sync_api import sync_playwright

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="reservoir_video_"))
    segs = (short_segments if a.cut == "short" else build_segments)(facts())
    stem = "reservoir_walkthrough_2min" if a.cut == "short" else "reservoir_walkthrough"

    print("narration:")
    for i, s in enumerate(segs):
        s.wav = work / f"seg{i:02d}.wav"
        s.dur = speak(s.say, s.wav, a.voice, a.rate)
        print(f"  {i:02d} {s.chapter:<38} {s.dur:5.1f}s")
    print(f"  narration total {sum(s.dur for s in segs):.0f}s (+ pauses and scene changes)")

    env = {**os.environ, "RESERVOIR_DEMO_REDACT": "1"}
    app = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "streamlit_app.py"),
                            "--server.headless", "true", "--server.port", str(PORT), "--browser.gatherUsageStats", "false"],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if urllib.request.urlopen(f"http://localhost:{PORT}/_stcore/health", timeout=2).status == 200:
                    break
            except Exception:
                time.sleep(1)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(work),
                                      record_video_size={"width": W, "height": H})
            page = ctx.new_page()
            t0 = time.monotonic()
            page.goto(f"http://localhost:{PORT}", wait_until="domcontentloaded")
            page.get_by_text("Sites shown").first.wait_for(timeout=300_000)
            page.add_style_tag(content=HIDE_CHROME_CSS)
            settle(page)
            page.wait_for_timeout(4000)   # map tiles and folium iframe
            title_card(page, "Reservoir", "")
            page.wait_for_timeout(800)

            shots = OUT / "frames"
            shots.mkdir(exist_ok=True)
            for i, s in enumerate(segs):
                s.start = time.monotonic() - t0
                caps = split_captions(s)
                todo = sorted(s.actions, key=lambda x: x[0])
                ci, shot_taken = -1, False
                while True:
                    el = time.monotonic() - t0 - s.start
                    while todo and todo[0][0] <= el:
                        try:
                            todo.pop(0)[1](page)
                        except Exception as e:      # keep recording; report what didn't happen
                            print(f"  ! segment {i} action failed: {type(e).__name__}: {str(e)[:160]}")
                    nci = max([k for k, (off, _) in enumerate(caps) if off <= el], default=0)
                    if nci != ci:
                        ci = nci
                        page.evaluate(OVERLAY_JS, {"cap": caps[ci][1], "chap": s.chapter if i not in (0, len(segs) - 1) else ""})
                    if not shot_taken and el > s.dur * 0.6:
                        page.screenshot(path=str(shots / f"seg{i:02d}.png"))
                        shot_taken = True
                    if el >= s.dur + a.pad and not todo:
                        break
                    page.wait_for_timeout(100)
            page.wait_for_timeout(1500)
            end = time.monotonic() - t0
            for i, s in enumerate(segs):
                nxt = segs[i + 1].start if i + 1 < len(segs) else end
                print(f"  scene {i:02d} {s.chapter:<30} narration {s.dur:5.1f}s  on screen {nxt - s.start:5.1f}s")
            video_path = Path(page.video.path())
            ctx.close()
            browser.close()
    finally:
        app.terminate()

    # Trim the loading period, lay each narration segment at its start time, encode H.264 + AAC.
    trim = max(segs[0].start - 0.2, 0)
    inputs, filters = ["-ss", f"{trim:.3f}", "-i", str(video_path)], []
    for k, s in enumerate(segs, start=1):
        inputs += ["-i", str(s.wav)]
        ms = int((s.start - trim) * 1000)
        filters.append(f"[{k}:a]adelay={ms}|{ms}[a{k}]")
    mix = "".join(f"[a{k}]" for k in range(1, len(segs) + 1)) + f"amix=inputs={len(segs)}:normalize=0[aout]"
    mp4 = OUT / f"{stem}.mp4"
    subprocess.run([ffmpeg, "-y", *inputs, "-filter_complex", ";".join(filters + [mix]), "-map", "0:v", "-map", "[aout]",
                    "-t", f"{end - trim:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
                    "-r", "25", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(mp4)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Script + subtitles for re-recording the voice-over yourself.
    lines, srt, n = ["# Reservoir walkthrough: narration script", ""], [], 1
    for s in segs:
        lines += [f"## {s.chapter}  ({srt_time(s.start - trim)[:8]})", "", s.say, ""]
        for off, text in split_captions(s):
            if not text:
                continue
            st_ = s.start - trim + off
            nxt = [o for o, _ in split_captions(s) if o > off]
            en = s.start - trim + (nxt[0] if nxt else s.dur)
            srt += [str(n), f"{srt_time(st_)} --> {srt_time(en)}", text, ""]
            n += 1
    (OUT / f"{stem}_script.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT / f"{stem}.srt").write_text("\n".join(srt), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{mp4}  ({end - trim:.0f}s)\nframes for review: {shots}")


if __name__ == "__main__":
    main()
