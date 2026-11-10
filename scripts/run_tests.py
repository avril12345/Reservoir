"""Run the Reservoir test suite in chunks and write a report.

    uv run python scripts/run_tests.py                       # default: unit pipeline extensibility outputs
    uv run python scripts/run_tests.py unit                  # one chunk
    uv run python scripts/run_tests.py pipeline outputs      # several chunks, one report
    uv run python scripts/run_tests.py outputs --region ma_ngrid_blackstone
    uv run python scripts/run_tests.py live                  # network checks of every configured source
    uv run python scripts/run_tests.py all                   # everything, including live and the app
    uv run python scripts/run_tests.py pipeline --update-golden   # accept intended score changes

Chunks: unit, pipeline, outputs, extensibility, live, app. The report (report.md / .html / .json) goes to
test_reports/<timestamp>_<chunks>/ unless --report-dir is given; test_reports/LATEST.txt points to the newest.
Any other arguments are passed to pytest (e.g. -k assemblage, -x, -v).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHUNKS = ["unit", "pipeline", "outputs", "extensibility", "live", "app"]
DEFAULT = ["unit", "pipeline", "extensibility", "outputs"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("chunks", nargs="*", help=f"any of {CHUNKS}, or 'all' (default: {' '.join(DEFAULT)})")
    p.add_argument("--region", help="limit region-parametrized tests to one region id")
    p.add_argument("--report-dir", help="where to write the report (default test_reports/<timestamp>_<chunks>/)")
    p.add_argument("--update-golden", action="store_true", help="rewrite tests/golden/ snapshots")
    a, extra = p.parse_known_args(argv)
    chunks = CHUNKS if a.chunks == ["all"] else (a.chunks or DEFAULT)
    bad = [c for c in chunks if c not in CHUNKS]
    if bad:
        p.error(f"unknown chunk(s) {bad}; choose from {CHUNKS} or 'all'")

    import pytest

    args = [str(ROOT / "tests"), "-m", " or ".join(chunks), "-p", "no:cacheprovider", "-q", "-rfEx"]
    args += ["--report-dir", a.report_dir] if a.report_dir else ["--report"]
    if "live" in chunks:
        args.append("--live")
    if a.region:
        args += ["--region", a.region]
    if a.update_golden:
        args.append("--update-golden")
    return int(pytest.main(args + extra))


if __name__ == "__main__":
    sys.exit(main())
