"""Turn per-parcel metrics into a 0–100 score, hard-filter pass/fail, and human-readable flags."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .bylaws import tracker
from .config import Region


def _lin(x, lo, hi):
    """1 at <=lo, 0 at >=hi, linear between (for distances)."""
    return float(np.clip((hi - x) / (hi - lo), 0, 1)) if pd.notna(x) else np.nan


def score(region: Region, df: pd.DataFrame) -> pd.DataFrame:
    w = region.weights
    pj, gw, ws = w["project"], w["grid"], w["weights"]
    sts, ps = w["site_type_scores"], w["permitting_scores"]
    smart_types = set(region.sources.get("smart", {}).get("priority_site_types", []))
    towns = tracker.load()
    out = df.copy()

    recs = []
    for _, r in out.iterrows():
        flags: list[str] = []
        mw = min(r.largest_contig_acres / pj["acres_per_mwac"], pj["max_mwac"])
        lo, hi = pj["target_mwac"]
        c_size = float(np.clip(mw / lo, 0, 1)) if mw < lo else 1.0

        c_site = sts.get(r.site_type, sts["other"])

        hc_mw = r.get("hc_mw")
        if pd.isna(hc_mw):
            c_hc = 0.5
            flags.append("Hosting capacity unknown (no utility HC layer for this territory)")
        else:
            c_hc = float(np.clip(hc_mw / max(mw, 0.25), 0, 1))
            if hc_mw <= 0:
                flags.append(f"Feeder {r.get('feeder_id')} shows 0 MW headroom — expect upgrade costs / group study")
            elif hc_mw < mw:
                flags.append(f"Feeder headroom {hc_mw:.2f} MW < est. {mw:.1f} MWac — downsize or add storage")
        yes = lambda v: str(v or "").strip().lower() not in ("", "none", "nan", "no", "n", "0")
        if yes(r.get("Ongoing_ASO")):
            flags.append("Ongoing ISO-NE Affected System Operator (ASO) study on this feeder's substation — timeline risk")
        elif yes(r.get("Potential_ASO")):
            flags.append("Potential ASO study area — check before committing")
        if yes(r.get("CIP_FEE")):
            flags.append(f"Capital Investment Project (CIP) fee area: {r.get('CIP_FEE')}")

        d = r.get("dist_3ph_m")
        c_dist = _lin(d, gw["full_credit_m"], gw["zero_credit_m"])
        if np.isnan(c_dist):
            c_dist = 0.0
            flags.append("No 3-phase feeder within 5 km")
        ds = r.get("dist_sub_m")
        if pd.notna(ds) and ds <= gw["substation_bonus_m"]:
            c_dist = min(1.0, c_dist + 0.1)

        rec = towns.get(tracker.key(region.state, str(r["_town"])))
        c_perm, pflags, perm_label = tracker.permitting_assessment(rec, ps)
        flags += pflags

        inc = 0.0
        if r.site_type in smart_types:
            inc += 0.5
        if r.get("energy_community"):
            inc += 0.25
        if r.get("low_income"):
            inc += 0.25
        c_inc = min(inc, 1.0)

        c_land = 1.0 - float(r.forest_share_buildable or 0)
        if r.forest_share_buildable > 0.5:
            flags.append(f"{r.forest_share_buildable:.0%} of buildable area is forest — clearing penalties likely")
        if r.wetland_share > 0.25:
            flags.append(f"{r.wetland_share:.0%} wetlands on parcel")
        if r.flood_share > 0.10:
            flags.append(f"{r.flood_share:.0%} in FEMA SFHA")
        if r.open_space_share > 0.0:
            flags.append(f"{r.open_space_share:.0%} protected open space (excluded)")
        if r.site_type == "landfill":
            flags.append(f"Landfill: {r.landfill_label or 'MassDEP solid-waste layer'} — check cap status / post-closure use permit")
        if r.site_type == "brownfield":
            flags.append(f"Brownfield: {r.brownfield_rtns} — check AUL limits on ground disturbance")

        comps = {"site_type": c_site, "grid_hosting": c_hc, "grid_distance": c_dist, "size": c_size,
                 "permitting": c_perm, "incentives": c_inc, "land_cover": c_land}
        total = 100 * sum(ws[k] * v for k, v in comps.items()) / sum(ws.values())
        if r.get("conservation_use") and r.site_type in smart_types:
            flags.append("Assessor lists this as conservation land — confirm Article 97 status before outreach")
        min_ac = pj["min_contiguous_acres_priority"] if r.site_type in smart_types else pj["min_contiguous_acres"]
        passes = (r.largest_contig_acres >= min_ac and r.open_space_share < 0.5
                  and r.site_type not in ("residential", "conservation", "excluded_use"))
        recs.append({"est_mwac": round(mw, 2), "score": round(total, 1), "passes_filters": passes,
                     "permitting_status": perm_label, "site_flags": flags,
                     **{f"c_{k}": round(v, 3) for k, v in comps.items()}})
    s = pd.DataFrame(recs, index=out.index)
    out = out.join(s)
    out["rank"] = out["score"].where(out.passes_filters).rank(ascending=False, method="first")
    return out.sort_values(["passes_filters", "score"], ascending=[False, False])
