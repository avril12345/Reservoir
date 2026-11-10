"""Pytest plugin: writes a test report (Markdown, HTML, JSON) for whatever chunk of the suite ran.

    pytest -m pipeline --report                      # -> test_reports/<timestamp>_pipeline/
    pytest -m "unit or outputs" --report-dir out/r1  # explicit location

Besides pass/fail, tests can add data sections (tables or notes) through the `report` fixture, e.g. the
region readiness matrix or output summaries, so the report says *what* was found, not only *whether* it passed.
Reports carry counts and checks only, never owner names or mailing addresses.
"""
from __future__ import annotations

import html
import json
import platform
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITES = ("unit", "pipeline", "outputs", "extensibility", "live", "app")


class Report:
    def __init__(self):
        self.results: dict[str, dict] = {}
        self.sections: dict[str, list] = defaultdict(list)   # title -> list of tables / notes
        self.started = time.time()

    # ---- used by tests through the `report` fixture
    def table(self, title: str, rows: list[dict], note: str = ""):
        self.sections[title].append({"kind": "table", "rows": rows, "note": note})

    def note(self, title: str, text: str):
        self.sections[title].append({"kind": "note", "text": text})

    def row(self, title: str, row: dict):
        """Append one row to a shared table (parametrized tests each add their own row)."""
        for s in self.sections[title]:
            if s["kind"] == "table" and s.get("shared"):
                s["rows"].append(row)
                return
        self.sections[title].append({"kind": "table", "rows": [row], "note": "", "shared": True})


_REPORT = Report()


def _suite_of(item) -> str:
    return next((m.name for m in item.iter_markers() if m.name in SUITES), "unmarked")


def pytest_addoption(parser):
    g = parser.getgroup("reservoir report")
    g.addoption("--report", action="store_true", help="write a test report to test_reports/<timestamp>_<chunk>/")
    g.addoption("--report-dir", default=None, help="write the test report to this directory")


@pytest.fixture
def report():
    return _REPORT


@pytest.hookimpl(trylast=True)   # after conftest has attached chunk markers
def pytest_collection_modifyitems(session, config, items):
    for it in items:
        _REPORT.results[it.nodeid] = {"suite": _suite_of(it), "outcome": "not run", "duration": 0.0, "message": ""}


def pytest_runtest_logreport(report):
    r = _REPORT.results.setdefault(report.nodeid, {"suite": "unmarked", "outcome": "not run", "duration": 0.0, "message": ""})
    r["duration"] += report.duration
    xfail = hasattr(report, "wasxfail")
    if report.when == "call" or (report.when == "setup" and (report.failed or report.skipped)) or (report.when == "teardown" and report.failed):
        if xfail:
            r["outcome"] = "xfailed" if report.skipped else "xpassed"
            r["message"] = report.wasxfail
        elif report.skipped:
            r["outcome"] = "skipped"
            r["message"] = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        elif report.failed:
            r["outcome"] = "error" if report.when != "call" else "failed"
            crash = getattr(report.longrepr, "reprcrash", None)
            r["message"] = crash.message if crash else str(report.longrepr)[-1500:]
            r["detail"] = str(report.longrepr)[-4000:]
        elif report.when == "call":
            r["outcome"] = "passed"


def _git() -> dict:
    def run(*a):
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    dirty = [l[3:] for l in run("status", "--porcelain").splitlines() if l and not l[3:].startswith("test_reports")]
    return {"commit": run("rev-parse", "--short", "HEAD"), "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "uncommitted_files": dirty}


def _versions() -> dict:
    from importlib.metadata import PackageNotFoundError, version

    out = {"python": platform.python_version()}
    for p in ("geopandas", "shapely", "pandas", "numpy", "rasterio", "pyproj", "pydantic", "streamlit", "anthropic", "pytest"):
        try:
            out[p] = version(p)
        except PackageNotFoundError:
            pass
    return out


def pytest_sessionfinish(session, exitstatus):
    cfg = session.config
    if not (cfg.getoption("--report") or cfg.getoption("--report-dir")):
        return
    ran = {k: v for k, v in _REPORT.results.items() if v["outcome"] != "not run"}
    if not ran:
        return
    chunk = cfg.getoption("-m") or "all"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() else "_" for c in chunk).strip("_")[:40] or "all"
    out = Path(cfg.getoption("--report-dir") or ROOT / "test_reports" / f"{stamp}_{slug}")
    out.mkdir(parents=True, exist_ok=True)

    by_suite: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for r in ran.values():
        by_suite[r["suite"]][r["outcome"]] += 1
        by_suite[r["suite"]]["seconds"] += r["duration"]
    total = defaultdict(int)
    for r in ran.values():
        total[r["outcome"]] += 1
    verdict = "PASS" if not (total["failed"] or total["error"]) else "FAIL"
    meta = {"generated": datetime.now().isoformat(timespec="seconds"), "chunk": chunk, "verdict": verdict,
            "args": sys.argv[1:], "region_filter": cfg.getoption("--region", None), "live": cfg.getoption("--live", False),
            "seconds": round(time.time() - _REPORT.started, 1), "git": _git(), "versions": _versions()}
    data = {"meta": meta, "totals": dict(total),
            "suites": {s: {k: (round(v, 2) if k == "seconds" else v) for k, v in d.items()} for s, d in by_suite.items()},
            "tests": ran, "sections": _REPORT.sections}
    (out / "report.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    md = _markdown(data)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "report.html").write_text(_html(data), encoding="utf-8")
    latest = ROOT / "test_reports"
    if out.parent == latest:
        (latest / "LATEST.txt").write_text(str(out.relative_to(ROOT)) + "\n", encoding="utf-8")
    tr = cfg.pluginmanager.get_plugin("terminalreporter")
    if tr:
        tr.write_line(f"\nreservoir test report ({verdict}) -> {out / 'report.md'}")


# ----------------------------------------------------------------------------------- rendering
ICON = {"passed": "PASS", "failed": "FAIL", "error": "ERROR", "skipped": "skip", "xfailed": "known gap", "xpassed": "XPASS"}


def _cell(v) -> str:
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 1000 else f"{v:,.0f}"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _md_table(rows: list[dict]) -> str:
    if not rows:
        return "_(no rows)_\n"
    cols = list(dict.fromkeys(k for r in rows for k in r))
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_cell(r.get(c, "")) for c in cols) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def _markdown(d: dict) -> str:
    m = d["meta"]
    g = m["git"]
    s = [f"# Reservoir test report: {m['verdict']}", "",
         f"- **Generated:** {m['generated']} ({m['seconds']} s)",
         f"- **Chunk:** `{m['chunk']}`" + (f" · region `{m['region_filter']}`" if m["region_filter"] else "") + (" · live network checks on" if m["live"] else ""),
         f"- **Code:** commit `{g['commit']}` on `{g['branch']}`" + (f", **{len(g['uncommitted_files'])} uncommitted files**" if g["uncommitted_files"] else ", clean tree"),
         "- **Versions:** " + ", ".join(f"{k} {v}" for k, v in m["versions"].items()), "",
         "## Summary by chunk", ""]
    rows = []
    for suite in [*SUITES, "unmarked"]:
        if suite in d["suites"]:
            x = d["suites"][suite]
            rows.append({"chunk": suite, **{ICON[k]: x.get(k, 0) for k in ICON}, "seconds": x.get("seconds", 0)})
    s += [_md_table(rows)]
    bad = {k: v for k, v in d["tests"].items() if v["outcome"] in ("failed", "error", "xpassed")}
    if bad:
        s += ["## Failures", ""]
        for k, v in bad.items():
            s += [f"### {ICON[v['outcome']]} `{k}`", "", "```", v["message"].strip()[:1500], "```", ""]
    gaps = {k: v for k, v in d["tests"].items() if v["outcome"] == "xfailed"}
    if gaps:
        s += ["## Known gaps (expected failures)", "", "Checks that are expected to fail, each with what's missing: readiness gaps for regions not marked "
              "`status: production`, and bugs the suite found that aren't fixed yet. A fixed bug shows up as XPASS, as a reminder to remove its marker.", ""]
        s += [_md_table([{"test": k.split("::")[-1], "gap": v["message"]} for k, v in gaps.items()])]
    for title, items in d["sections"].items():
        s += [f"## {title}", ""]
        for it in items:
            if it["kind"] == "note":
                s += [it["text"], ""]
            else:
                if it.get("note"):
                    s += [it["note"], ""]
                s += [_md_table(it["rows"])]
    s += ["## All tests", ""]
    s += [_md_table([{"chunk": v["suite"], "result": ICON.get(v["outcome"], v["outcome"]), "test": k, "s": round(v["duration"], 2)}
                     for k, v in sorted(d["tests"].items(), key=lambda kv: (kv[1]["suite"], kv[0]))])]
    return "\n".join(s)


def _html(d: dict) -> str:
    m, e = d["meta"], html.escape
    colour = {"PASS": "#1e7d3c", "FAIL": "#b3261e", "ERROR": "#b3261e", "known gap": "#8a6d00", "skip": "#666", "XPASS": "#b3261e"}

    def table(rows):
        if not rows:
            return "<p><em>(no rows)</em></p>"
        cols = list(dict.fromkeys(k for r in rows for k in r))
        head = "".join(f"<th>{e(c)}</th>" for c in cols)
        body = ""
        for r in rows:
            tds = ""
            for c in cols:
                v = _cell(r.get(c, "")).replace("\\|", "|")
                style = f' style="color:{colour[v]};font-weight:600"' if v in colour else ""
                tds += f"<td{style}>{e(v)}</td>"
            body += f"<tr>{tds}</tr>"
        return f"<div class=tw><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"

    parts = [f"<h1>Reservoir test report: <span style='color:{colour.get(m['verdict'], '#000')}'>{m['verdict']}</span></h1>",
             f"<p>{e(m['generated'])} · chunk <code>{e(m['chunk'])}</code> · commit <code>{e(m['git']['commit'])}</code>"
             + (f" + {len(m['git']['uncommitted_files'])} uncommitted files" if m["git"]["uncommitted_files"] else "") + f" · {m['seconds']} s</p>"]
    rows = [{"chunk": s, **{ICON[k]: x.get(k, 0) for k in ICON}, "seconds": x.get("seconds", 0)} for s, x in d["suites"].items()]
    parts += ["<h2>Summary by chunk</h2>", table(rows)]
    bad = {k: v for k, v in d["tests"].items() if v["outcome"] in ("failed", "error", "xpassed")}
    if bad:
        parts.append("<h2>Failures</h2>")
        for k, v in bad.items():
            parts.append(f"<details open><summary><b>{e(k)}</b></summary><pre>{e(v.get('detail') or v['message'])}</pre></details>")
    gaps = [{"test": k.split("::")[-1], "gap": v["message"]} for k, v in d["tests"].items() if v["outcome"] == "xfailed"]
    if gaps:
        parts += ["<h2>Known gaps (expected failures)</h2>", table(gaps)]
    for title, items in d["sections"].items():
        parts.append(f"<h2>{e(title)}</h2>")
        for it in items:
            if it["kind"] == "note":
                parts.append(f"<p>{e(it['text'])}</p>")
            else:
                if it.get("note"):
                    parts.append(f"<p>{e(it['note'])}</p>")
                parts.append(table(it["rows"]))
    parts += ["<h2>All tests</h2>", table([{"chunk": v["suite"], "result": ICON.get(v["outcome"], v["outcome"]), "test": k, "s": round(v["duration"], 2)}
                                          for k, v in sorted(d["tests"].items(), key=lambda kv: (kv[1]["suite"], kv[0]))])]
    css = ("body{font:14px/1.45 system-ui,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px;color:#1b1b1b;background:#fff}"
           "table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}"
           "th{background:#f3f3f3}.tw{overflow-x:auto}pre{white-space:pre-wrap;background:#f7f7f7;padding:8px;font-size:12px}code{background:#f3f3f3;padding:0 3px}")
    return f"<!doctype html><meta charset=utf-8><title>Reservoir test report</title><style>{css}</style>{''.join(parts)}"
