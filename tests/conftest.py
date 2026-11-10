"""Shared test setup: chunk markers, command-line options and fixtures.

Chunks (run any subset with -m, or through scripts/run_tests.py):
  unit           pure functions: use codes, Esri geometry, assemblages, tracker logic, scoring math
  pipeline       a synthetic region with known answers, run offline through analyse -> score -> outputs
  outputs        checks on the real outputs/<region>/ produced by `reservoir run`
  extensibility  config contract for every region/state, plus readiness of each region to run
  live           every configured ArcGIS URL responds and has the fields the config names (needs --live)
  app            the Streamlit app renders (needs a completed run)
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Older test files carry no marker of their own.
SUITE_BY_FILE = {"test_core.py": "unit", "test_extract.py": "unit", "test_app.py": "app"}


def pytest_addoption(parser):
    g = parser.getgroup("reservoir")
    g.addoption("--live", action="store_true", help="run the live network checks against configured sources")
    g.addoption("--region", default=None, help="only run region-parametrized tests for this region id")
    g.addoption("--update-golden", action="store_true", help="rewrite tests/golden/ snapshots from this run")


def pytest_configure(config):
    for name, doc in [("unit", "pure-function tests"), ("pipeline", "synthetic end-to-end pipeline"),
                      ("outputs", "validity of real region outputs"), ("extensibility", "config contract and region readiness"),
                      ("live", "network checks of configured sources (opt-in with --live)"), ("app", "Streamlit app smoke test")]:
        config.addinivalue_line("markers", f"{name}: {doc}")


def pytest_collection_modifyitems(config, items):
    region = config.getoption("--region")
    keep, drop = [], []
    for it in items:
        suite = SUITE_BY_FILE.get(Path(str(it.fspath)).name)
        if suite and not any(m.name == suite for m in it.iter_markers()):
            it.add_marker(getattr(pytest.mark, suite))
        if any(m.name == "live" for m in it.iter_markers()) and not config.getoption("--live"):
            it.add_marker(pytest.mark.skip(reason="live network check: pass --live to run"))
        params = getattr(getattr(it, "callspec", None), "params", {})
        if region and "region_id" in params and params["region_id"] != region:
            drop.append(it)
        else:
            keep.append(it)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


@pytest.fixture
def update_golden(request) -> bool:
    return request.config.getoption("--update-golden")
