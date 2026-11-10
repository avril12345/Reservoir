"""Live chunk (opt-in, needs network): every source URL in config/sources/*.yaml responds, and every field the
config names exists on that layer. Run this when adding or changing a state's sources, or when a public
server may have moved a layer (HIFLD Open going offline in 2025 is the kind of thing it catches).

    pytest -m live --live
    pytest -m live --live -k ny                # one state's sources

Results are not cached and depend on third-party servers, so a failure here is a finding about a source,
not necessarily about the code.
"""
from __future__ import annotations

import re

import pytest
import requests

from reservoir.arcgis import USER_AGENT
from reservoir.config import CONFIG, _load

pytestmark = pytest.mark.live
TIMEOUT = 45
WHERE_FIELD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:IN\b|LIKE\b|=|>|<)", re.I)


def _where_fields(where: str | None) -> list[str]:
    return [f for f in WHERE_FIELD.findall(where or "") if f.upper() not in ("UPPER", "AND", "OR", "NOT")]


def _sources() -> list[tuple[str, str, str, list[str]]]:
    """(state, layer key, url, required fields) for every ArcGIS layer the config points at."""
    out = []
    for path in sorted((CONFIG / "sources").glob("*.yaml")):
        st = path.stem.upper()
        s = _load(path)
        for key, cfg in s.items():
            if not isinstance(cfg, dict) or key in ("hosting_capacity", "smart"):
                continue
            if key in ("dem_3dep", "landcover_nlcd"):
                continue
            url = cfg.get("url")
            if not url:
                continue
            need = [*cfg.get("label_fields", []), *(cfg.get("fields") if isinstance(cfg.get("fields"), list) else [])]
            for k in ("name_field", "id_field", "town_field", "utility_field", "county_field", "town_id_field",
                      "site_address_field", "use_code_field", "use_desc_field", "map_par_field"):
                if cfg.get(k):
                    need.append(cfg[k])
            need += [v for v in (cfg.get("owner_fields") or {}).values() if v]
            need += _where_fields(cfg.get("exclude_where")) + _where_fields(cfg.get("income_where")) + _where_fields(cfg.get("town_where"))
            if key == "parcels":
                need.append("Shape__Area")   # fetch.py filters parcels on it
            out.append((st, key, url, sorted(set(need))))
        for util, hc in (s.get("hosting_capacity") or {}).items():
            if hc.get("feeders_url"):
                need = [hc[k] for k in ("feeder_id", "substation", "voltage_kv", "hc_mw", "hc_mw_alt", "updated") if hc.get(k)]
                out.append((st, f"feeders[{util}]", hc["feeders_url"], need))
            if hc.get("substations_url"):
                out.append((st, f"substations[{util}]", hc["substations_url"], [hc["substation_name"]] if hc.get("substation_name") else []))
    return out


SOURCES = _sources()


@pytest.mark.parametrize("state,key,url,fields", SOURCES, ids=[f"{s.lower()}-{k}" for s, k, _, _ in SOURCES])
def test_layer_responds_with_configured_fields(state, key, url, fields, report):
    t0 = __import__("time").time()
    try:
        r = requests.get(url, params={"f": "json"}, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        info = r.json()
    except Exception as exc:
        report.row("Live: source checks", {"state": state, "layer": key, "result": "FAIL", "detail": f"{type(exc).__name__}"})
        raise
    secs = round(__import__("time").time() - t0, 1)
    if "error" in info:
        report.row("Live: source checks", {"state": state, "layer": key, "result": "FAIL", "s": secs, "detail": str(info["error"])[:120]})
        pytest.fail(f"{url}: {info['error']}")
    have = {f["name"].upper() for f in info.get("fields") or []}
    missing = [f for f in fields if f.upper() not in have] if have else []
    report.row("Live: source checks", {"state": state, "layer": key, "result": "FAIL" if missing else "PASS", "s": secs,
                                       "fields checked": len(fields), "detail": f"missing {missing}" if missing else info.get("name", "")})
    assert have or not fields, f"{url} returned no field list"
    assert not missing, f"{state} {key}: fields named in config but not on the layer: {missing}"


def test_3dep_image_server():
    n = _load(CONFIG / "sources" / "national.yaml")
    info = requests.get(n["dem_3dep"]["url"], params={"f": "json"}, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT).json()
    assert "error" not in info and info.get("bandCount", 1) >= 1


def test_nlcd_wcs_offers_coverage():
    lc = _load(CONFIG / "sources" / "national.yaml")["landcover_nlcd"]
    r = requests.get(lc["wcs"], params={"service": "WCS", "request": "GetCapabilities", "version": "2.0.1"},
                     headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    assert lc["coverage"] in r.text, f"coverage {lc['coverage']} not offered by the WCS"
