"""Offline test of the Claude extraction path: a local mock of the Messages API returns a canned
structured response, so we exercise the real SDK call, schema parsing, quote verification and
tracker conversion without an API key. The canned answer deliberately contains one hallucinated
setback quote to prove it gets demoted."""
import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import anthropic
import pytest

from reservoir.bylaws import extract

DOC = Path(__file__).resolve().parent.parent / "data" / "bylaws" / "docs" / "ny_cortlandt_ll1_2026.txt"

EV = lambda q, s="": {"quote": q, "section": s}
CANNED = {
    "municipality": "Cortlandt", "state": "NY", "document_title": "Local Law No. 1 of 2026", "document_date": "2026-07-21",
    "solar_ground_mount": {"permission": "not_addressed", "districts_allowed": [], "max_size_text": "", "max_size_mw": None, "evidence": EV("")},
    "bess": {"permission": "not_addressed", "districts_allowed": [], "max_size_text": "", "max_size_mw": None,
             "evidence": EV("Currently, the Town Code is devoid of provisions directly related to Battery Energy Storage.", "§ 1")},
    "solar_setbacks": {"front_ft": None, "side_ft": None, "rear_ft": None, "from_residence_ft": None, "evidence": EV("")},
    # Hallucination: this sentence is not in the document.
    "bess_setbacks": {"front_ft": 100, "side_ft": 100, "rear_ft": 100, "from_residence_ft": None,
                      "evidence": EV("Battery Energy Storage Systems shall be set back 100 feet from all lot lines.", "§ 4")},
    "decommissioning": {"required": None, "financial_surety_required": None, "evidence": EV("")},
    "moratoria": [{
        "technology": "bess", "local_law": "Local Law No. 1 of 2026", "status": "extended", "adopted": "2026-07-21",
        "effective": None, "expires": "2026-12-31", "duration_days": None,
        "scope": "No applications considered or approvals granted for new commercial BESS.",
        "evidence": EV("The moratorium herein shall be extended through December 31, 2026 unless terminated earlier.", "§ 3"),
    }],
    "ma_doer_model_solar_bylaw": None, "ma_doer_model_bess_bylaw": None, "ma_consolidated_permit_bylaw": None,
    "notes": "Extension of prior moratoria (LL 2-2024, 10-2024, 2-2025, 7-2025).",
}


class _Handler(BaseHTTPRequestHandler):
    captured: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _Handler.captured.append({"body": body, "headers": dict(self.headers)})
        resp = {"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": json.dumps(CANNED)}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 500}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("request-id", "req_test")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def mock_api():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    _Handler.captured.clear()
    yield anthropic.Anthropic(api_key="test", base_url=f"http://127.0.0.1:{srv.server_port}", max_retries=0)
    srv.shutdown()


def test_extract_end_to_end_with_quote_verification(mock_api):
    text, _ = extract.load_source(str(DOC))
    result = extract.extract(text, "NY", "Cortlandt", client=mock_api)

    req = _Handler.captured[0]["body"]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in _Handler.captured[0]["headers"].get("anthropic-beta", "")

    checks = extract.verify(result, text)
    assert checks["moratoria[0]"] == "verbatim"
    assert checks["bess"] == "verbatim"
    assert checks["bess_setbacks"] == "not_found"          # hallucinated quote caught

    rec = extract.to_record(result, checks, str(DOC))
    assert rec.bess_setback_ft is None                       # demoted, not trusted
    assert rec.bess_permission == "not_addressed"
    assert rec.moratoria[0].expires == date(2026, 12, 31)


def test_quote_check_tolerates_pdf_whitespace():
    src = extract._norm("The moratorium herein shall be extended through Decem-\nber 31, 2026 unless   terminated earlier.")
    assert extract.check_quote("The moratorium herein shall be extended through December 31, 2026 unless terminated earlier.", src) == "verbatim"
    assert extract.check_quote("The moratorium shall last 365 days.", src) == "not_found"


def test_failed_quote_check_never_reaches_geometry(monkeypatch):
    """A solar setback whose quote isn't in the source is dropped by to_record, so geometry uses the default."""
    import copy

    from reservoir.analysis import property_line_m
    from reservoir.bylaws import tracker
    from reservoir.bylaws.schema import ExtractionResult
    from reservoir.config import load_region

    bad = copy.deepcopy(CANNED)
    bad["solar_setbacks"] = {"front_ft": 100, "side_ft": 100, "rear_ft": 100, "from_residence_ft": 300,
                             "evidence": EV("Solar arrays shall be set back 100 feet from every lot line.", "§ 9")}
    result = ExtractionResult.model_validate(bad)
    text, _ = extract.load_source(str(DOC))
    checks = extract.verify(result, text)
    assert checks["solar_setbacks"] == "not_found"
    rec = extract.to_record(result, checks, str(DOC))
    assert rec.solar_setback_ft is None and rec.from_residence_ft is None
    region = load_region("ma_ngrid_blackstone")
    metres, source, _ = property_line_m(region, "Cortlandt", {tracker.key(region.state, "Cortlandt"): rec.model_copy(update={"state": "MA"})})
    assert (metres, source) == (15, "default")
