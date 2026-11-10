"""Headless smoke test: the Streamlit app renders every tab without exceptions (needs a completed run)."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not any((ROOT / "outputs").glob("*/metrics.pkl")), reason="no screened region yet")


def test_app_renders():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=240).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.tabs) == 7
    assert any("Sites shown" in m.label for m in at.metric)
    assert any("Tax lots behind these sites" in e.label for e in at.expander)
