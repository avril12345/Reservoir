"""reservoir CLI.

    reservoir fetch  --region ma_ngrid_blackstone      # download + cache all layers
    reservoir score  --region ma_ngrid_blackstone      # analyse, score, write shortlist/owners/cards
    reservoir run    --region ma_ngrid_blackstone      # fetch + score
    reservoir bylaws seed                              # write seed tracker records
    reservoir bylaws extract --state NY --town Cortlandt --source data/bylaws/docs/ny_cortlandt_ll1_2026.txt
    reservoir bylaws worksheet                         # 20-town hand-check worksheet
    reservoir bylaws accuracy                          # score LLM extractions vs. hand-checked truth
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import time

from .config import list_regions, load_region


def cmd_fetch(a):
    r = load_region(a.region)
    if a.towns:
        r.towns = [t.upper() for t in a.towns]
    t = time.time()
    counts = __import__("reservoir.fetch", fromlist=["fetch_region"]).fetch_region(r)
    print(json.dumps(counts, indent=2), f"\nfetched in {time.time() - t:.0f}s")


def cmd_score(a):
    from . import analysis, outputs, scoring

    r = load_region(a.region, a.weights)
    t = time.time()
    metrics_cache = r.out_dir / "metrics.pkl"
    if metrics_cache.exists() and not a.reanalyse:
        df = pickle.loads(metrics_cache.read_bytes())
    else:
        df = analysis.analyse(r)
        metrics_cache.write_bytes(pickle.dumps(df))
    scored = scoring.score(r, df)
    short = outputs.write_shortlist(r, scored, a.top)
    owners = outputs.write_owner_list(r, short)
    cards = outputs.write_cards(r, short, a.cards)
    meta = {"region": r.id, "parcels_analysed": len(df), "passing_filters": int(scored.passes_filters.sum()),
            "shortlist": len(short), "owners": len(owners), "cards": len(cards), "weights": a.weights or "default",
            "generated": time.strftime("%Y-%m-%d %H:%M")}
    (r.out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2), f"\nscored in {time.time() - t:.0f}s -> {r.out_dir}")


def cmd_run(a):
    cmd_fetch(a)
    a.reanalyse = True
    cmd_score(a)


def cmd_bylaws(a):
    from .bylaws import accuracy, extract, seed

    if a.action == "seed":
        print("tracker records:", seed.write_seeds())
    elif a.action == "extract":
        rec, result = extract.extract_to_tracker(a.state, a.town, a.source, county=a.county or "")
        print(rec.model_dump_json(indent=2))
    elif a.action == "worksheet":
        print("wrote", accuracy.write_worksheet())
    elif a.action == "accuracy":
        print(json.dumps(accuracy.report(), indent=2, default=str))


def main(argv=None):
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="reservoir")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("fetch", cmd_fetch), ("score", cmd_score), ("run", cmd_run)):
        s = sub.add_parser(name)
        s.add_argument("--region", default="ma_ngrid_blackstone", choices=list_regions())
        s.add_argument("--towns", nargs="*", help="override region towns")
        s.add_argument("--weights", default=None, help="weights profile in config/weights")
        s.add_argument("--top", type=int, default=50)
        s.add_argument("--cards", type=int, default=25)
        s.add_argument("--reanalyse", action="store_true", help="recompute geometry metrics (after fetch or setback change)")
        s.set_defaults(fn=fn)
    b = sub.add_parser("bylaws")
    b.add_argument("action", choices=["seed", "extract", "worksheet", "accuracy"])
    b.add_argument("--state"); b.add_argument("--town"); b.add_argument("--county"); b.add_argument("--source", help="URL, PDF, or text file")
    b.set_defaults(fn=cmd_bylaws)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
