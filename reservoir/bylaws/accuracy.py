"""Human-verified accuracy check for the siting tracker.

Workflow
  1. `reservoir bylaws worksheet` writes data/bylaws/verification_worksheet.csv: 20 towns (stratified
     MA / NY, with/without moratoria), each row pre-filled with the tracker's current (LLM or seed)
     value per field and an empty `truth_*` column.
  2. A reviewer reads the primary source for each town and fills `truth_*`, `checked_by`, `checked_on`,
     optional `error_type` overrides, and saves it as data/bylaws/ground_truth.csv.
  3. `reservoir bylaws accuracy` scores field-level accuracy with Wilson 95% intervals and an error
     taxonomy (omission, hallucination, wrong value, stale, entity confusion, unit).

Nothing in this module invents ground truth: with an empty ground_truth.csv the report says so.
"""
from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from datetime import date

from ..config import ROOT
from . import tracker

DIR = ROOT / "data" / "bylaws"
WORKSHEET = DIR / "verification_worksheet.csv"
TRUTH = DIR / "ground_truth.csv"

FIELDS = {
    # field: comparison kind
    "solar_permission": "category",
    "bess_permission": "category",
    "solar_setback_ft": "number",
    "bess_setback_ft": "number",
    "decommissioning_required": "bool",
    "moratorium_active": "bool",
    "moratorium_expires": "date",
    "ma_consolidated_permit": "category",
}

# 20-town sample. MA: the 4 pilot towns + 9 more National Grid towns in the next expansion ring;
# NY: the 3 seeds + 4 Hudson Valley towns. Swap freely — keep the stratification (state × moratorium).
SAMPLE = [
    ("MA", "Uxbridge"), ("MA", "Douglas"), ("MA", "Northbridge"), ("MA", "Sutton"), ("MA", "Mendon"),
    ("MA", "Millville"), ("MA", "Blackstone"), ("MA", "Webster"), ("MA", "Dudley"), ("MA", "Oxford"),
    ("MA", "Charlton"), ("MA", "Grafton"), ("MA", "Upton"),
    ("NY", "Waterford"), ("NY", "Cortlandville"), ("NY", "Cortlandt"), ("NY", "Kinderhook"),
    ("NY", "Claverack"), ("NY", "Coxsackie"), ("NY", "Red Hook"),
]


def tracker_values(rec) -> dict:
    if rec is None:
        return {f: "" for f in FIELDS}
    act = rec.active_moratoria(date.today())
    return {
        "solar_permission": rec.solar_permission,
        "bess_permission": rec.bess_permission,
        "solar_setback_ft": rec.solar_setback_ft if rec.solar_setback_ft is not None else "",
        "bess_setback_ft": rec.bess_setback_ft if rec.bess_setback_ft is not None else "",
        "decommissioning_required": "" if rec.decommissioning_required is None else rec.decommissioning_required,
        "moratorium_active": bool(act),
        "moratorium_expires": next((m.expires.isoformat() for m in act if m.expires), ""),
        "ma_consolidated_permit": rec.ma_consolidated_permit,
    }


def write_worksheet() -> str:
    recs = tracker.load()
    cols = ["state", "municipality", "tracker_method", "source_urls"]
    for f in FIELDS:
        cols += [f"pred_{f}", f"truth_{f}"]
    cols += ["error_type", "checked_by", "checked_on", "truth_source_url", "notes"]
    DIR.mkdir(parents=True, exist_ok=True)
    with open(WORKSHEET, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for st, town in SAMPLE:
            rec = recs.get(tracker.key(st, town))
            row = {"state": st, "municipality": town, "tracker_method": rec.method if rec else "not_in_tracker",
                   "source_urls": " ".join(s.get("url", "") for s in (rec.sources if rec else []))}
            for f, v in tracker_values(rec).items():
                row[f"pred_{f}"] = v
            w.writerow(row)
    return str(WORKSHEET)


def _eq(kind: str, pred: str, truth: str) -> bool:
    p, t = (pred or "").strip().lower(), (truth or "").strip().lower()
    if kind == "number":
        try:
            return abs(float(p) - float(t)) <= 5.0   # ±5 ft tolerance
        except ValueError:
            return p == t
    if kind == "bool":
        norm = lambda s: {"true": "true", "yes": "true", "1": "true", "false": "false", "no": "false", "0": "false"}.get(s, s)
        return norm(p) == norm(t)
    return p == t


def _classify(kind: str, pred: str, truth: str) -> str:
    p, t = (pred or "").strip().lower(), (truth or "").strip().lower()
    empty = {"", "unknown", "none", "nan"}
    if p in empty and t not in empty and t != "not_addressed":
        return "omission"
    if (t in empty or t == "not_addressed") and p not in empty and p != "not_addressed":
        return "hallucination"
    return "wrong_value"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - a) / d, (c + a) / d)


def report() -> dict:
    if not TRUTH.exists():
        return {"status": "no ground truth yet", "hand_checked_towns": 0, "target": len(SAMPLE),
                "next_step": "run `reservoir bylaws worksheet`, fill the truth_* columns, and save as data/bylaws/ground_truth.csv"}
    rows = [r for r in csv.DictReader(open(TRUTH, encoding="utf-8")) if (r.get("checked_by") or "").strip()]
    per_field = {}
    errors = Counter()
    examples = defaultdict(list)
    for f, kind in FIELDS.items():
        k = n = 0
        for r in rows:
            t = r.get(f"truth_{f}", "")
            if t.strip() == "":
                continue           # reviewer left blank = not checked for this field
            n += 1
            p = r.get(f"pred_{f}", "")
            if _eq(kind, p, t):
                k += 1
            else:
                et = (r.get("error_type") or "").strip() or _classify(kind, p, t)
                errors[et] += 1
                examples[et].append(f"{r['state']}/{r['municipality']}.{f}: tracker={p!r} truth={t!r}")
        lo, hi = wilson(k, n)
        per_field[f] = {"correct": k, "checked": n, "accuracy": round(k / n, 3) if n else None,
                        "ci95": [round(lo, 3), round(hi, 3)] if n else None}
    tot_k = sum(v["correct"] for v in per_field.values())
    tot_n = sum(v["checked"] for v in per_field.values())
    lo, hi = wilson(tot_k, tot_n)
    return {"hand_checked_towns": len(rows), "target": len(SAMPLE),
            "overall": {"correct": tot_k, "checked": tot_n, "accuracy": round(tot_k / tot_n, 3) if tot_n else None, "ci95": [round(lo, 3), round(hi, 3)] if tot_n else None},
            "per_field": per_field, "error_types": dict(errors), "examples": {k: v[:5] for k, v in examples.items()}}
