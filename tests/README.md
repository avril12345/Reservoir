# Reservoir test suite

The suite runs in **chunks**. You can run one chunk or several, and each run writes a **report**
(`report.md`, `report.html` and `report.json`) to `test_reports/<timestamp>_<chunks>/`. The file
`test_reports/LATEST.txt` points to the newest report.

```
uv run python scripts/run_tests.py                          # unit + pipeline + extensibility + outputs (~40 s)
uv run python scripts/run_tests.py unit                     # one chunk
uv run python scripts/run_tests.py outputs --region ma_ngrid_blackstone
uv run python scripts/run_tests.py live                     # network checks of every configured source
uv run python scripts/run_tests.py all                      # everything (~95 s)
uv run python scripts/run_tests.py pipeline -k assemblage   # extra args go to pytest
uv run pytest -m "unit or pipeline" --report                # same thing through plain pytest
```

| Chunk | Tests | Needs | What it shows |
|---|---|---|---|
| `unit` | `test_units.py`, `test_core.py`, `test_extract.py` | nothing | Use codes, Esri geometry, assemblages, scoring math, a seeded sweep of 2,000 sites (bounds and monotonic behaviour), grid joins, slope, owner typing, the tracker store, quote checks |
| `pipeline` | `test_pipeline.py` | nothing | A synthetic two-town region (`synthetic.py`) runs offline through analyse → score → outputs, and is checked against hand-worked answers, a golden snapshot and a second identical run. It runs under both MA and NY config. |
| `extensibility` | `test_extensibility.py` | nothing | Checks that region and weights configs are valid; builds the readiness matrix of regions × requirements; tests adding towns (YAML, `--towns`, an offline fetch of 1 vs 3 towns, no score change in existing towns); runs the pipeline under every region's config and under a brand-new state that exists only as YAML; lists the state-specific places in the code |
| `outputs` | `test_outputs_validity.py` | a completed `reservoir run` | Checks the real `outputs/<region>/` files. Ranges, the score formula, MW sizing, filters, ranks, areas, IDs, geometry, flags, owner totals and cards must all be consistent. Feeder distances and landfill/brownfield evidence are recomputed from `inputs.gpkg`. A rescore of `metrics.pkl` is compared with the files to show whether outputs are **stale**. |
| `live` | `test_live_sources.py` | network, `--live` | Every URL in `config/sources/*.yaml` responds, and every field the config names exists on the layer. Run this when adding or changing a state. |
| `app` | `test_app.py` | a completed run | The Streamlit app renders all 7 tabs |

## Repeatability

- The synthetic data is built in code and nothing in it is random. The property sweep uses a fixed seed.
- Pipeline tests write only to pytest's temp directories (`synthetic.isolate`) and use an in-memory siting
  tracker with far-future dates, so they never read or write `outputs/` or `data/bylaws/tracker.jsonl`, and the
  result doesn't depend on today's date.
- `tests/golden/synthetic_{MA,NY}.csv` snapshots every synthetic site's type, filter result, rank, score and
  components. If you change the scoring on purpose, accept the new snapshot with
  `scripts/run_tests.py pipeline --update-golden` and commit `tests/golden/`.
- Each report records the commit, how many files are uncommitted, and the library versions.

## Results

- **FAIL / ERROR**: a real problem. The report shows the assertion and the values involved.
- **known gap** (xfail): an expected failure. It is either a readiness requirement missing from a region not
  marked `status: production` (set in `config/regions/*.yaml`), or a bug this suite found that isn't fixed yet.
  The reason is printed in the report.
- **XPASS**: a known gap now passes. Remove its marker (`KNOWN_GAPS` in `test_pipeline.py`).
- **skip**: the test's prerequisite is missing, such as a region that hasn't been run, or `--live` not passed.

## Adding a town or a state

1. **Town:** add it to `towns:` in the region YAML, or pass `--towns` once. Run `extensibility` and, after
   `reservoir run`, `outputs --region <id>`.
2. **State:** add `config/sources/<st>.yaml` and `config/regions/<id>.yaml` with `status: scaffold`. Then:
   - Run `live -k <st>` until every configured layer and field exists.
   - Run `extensibility --region <id>` and work down the readiness matrix. The "state-specific code
     touchpoints" table lists the code to extend (`site_type_from_use`, `TownRecord.state`).
   - Add an `ORIGINS` entry in `synthetic.py` if the state uses a new CRS.
   - Add the state to `USE_CODES` in `synthetic.py` and to `STATES` in `test_pipeline.py`, so the
     hand-worked answers run under its config.
   - Mark the region `status: production` once the readiness checks pass. From then on, any gap is a hard failure.
