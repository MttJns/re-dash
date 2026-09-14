# CLAUDE.md

Guidance for Claude Code sessions in this repository.

## What this is

re-dash is a single-pane residential real estate dashboard, live at https://re-dash.com. It currently covers
the Austin metro (CBSA 12420). It is a static site: a Python build fetches public data and writes JSON plus
HTML/CSS/JS into `dist/`, which is served from a private S3 bucket through CloudFront.

## Layout

| Path | Purpose |
|---|---|
| `build.py` | Fetches sources, writes `dist/` (stdlib only, Python 3.11+ for `tomllib`) |
| `metros.toml` | Metros and their source identifiers; financing assumptions |
| `site/` | `index.html`, `app.css`, `app.js` (hand-built SVG charts, no framework), favicons |
| `tests/test_build.py` | Parser unit tests |
| `deploy.sh` | Uploads `dist/` to S3 with cache headers, purges pointers, prunes old builds |
| `infra/` | CloudFormation (`dns.yaml`, `site.yaml`) and the setup runbook `infra/README.md` |
| `.github/workflows/build.yml` | Tests + build on push to `main` and Fridays; deploys via GitHub OIDC |

## Commands

```sh
python3 -m unittest discover -s tests      # tests; run before every commit
python3 build.py                           # build into dist/ (income is null without CENSUS_API_KEY)
python3 -m http.server 8765 --directory dist   # local preview
node --check site/app.js                   # JS syntax check
```

Deploys normally happen by pushing to `main`: the workflow builds and runs `deploy.sh`. `main` blocks force
pushes and deletion.

## Caching rules (do not break)

- Content-hashed `app.<hash>.css/js` and `data/<build>/...json` are immutable (`max-age=31536000, immutable`).
  Never overwrite a data file; every build writes a new `data/<build>/` folder.
- `index.html` and `data/manifest.json` are the only pointers: short cache, uploaded last, purged on deploy.
- Pointers and favicons stay at CloudFront for 30 days (`s-maxage=2592000`) and rely on the deploy invalidation;
  browsers recheck pointers every 60s and icons daily. Manual bucket changes need a manual invalidation.
- County view: selection lives in the URL hash (`#austin-tx/travis`; bare URL shows the metro's
  `default_region`; `#austin-tx` is the whole metro), so every region shares one cached `index.html`. Each region
  is one immutable JSON (`data/<build>/austin-tx.json`, `data/<build>/austin-tx/<county>.json`); the manifest
  lists `regions` and keeps each metro's `path` so pages cached before a deploy still load.

## Data sources and gotchas

- **Redfin** metro tracker TSV on S3: filter `TABLE_ID` = CBSA, `PROPERTY_TYPE` = All Residential,
  `IS_SEASONALLY_ADJUSTED` = false.
- **Zillow** ZHVI tiers and ZORI wide CSVs, matched on `RegionID`.
- **Mortgage rates** come from Freddie Mac's `PMMS_history.csv`. FRED's `fredgraph.csv` blocks or stalls
  scripted and CI requests; don't switch back to it.
- **Census building permits**: monthly files `cbsaYYMMc.txt` in "CBSA (beginning Jan 2024)"; units columns
  are indices 6 (1-unit), 9, 12, 15. County files (`coYYMMc.txt`) have a different layout.
- **Census population estimates**: migration series starts at 2021 (2020 covers only April-July).
- **Census ACS income** needs `CENSUS_API_KEY`; 1-year data only exists for areas with 65,000+ people.
- **County sources**: Redfin `county_market_tracker.tsv000.gz` (~240 MB, streamed; `TABLE_ID` is Redfin's own
  county id, not FIPS), Zillow `County_*` CSVs, BPS `County/coYYMMc.txt` (units at indices 7, 10, 13, 16),
  PEP `co-est{vintage}-alldata.csv` (`SUMLEV` 050). ACS income falls back to 5-year where 1-year is missing
  (Caldwell). The build warns if county permits or migration don't sum to the metro.
- Each view shows figures for one region: a county or the whole metro (five counties, not a ZIP code list).

## Conventions

- Python standard library only; keep the build dependency-free.
- Charts follow the dataviz rules already in `app.js`: thin marks, text in ink tokens not series colors,
  a table view for every chart, labels inserted with `textContent`.
- Keep the dashboard to one screen at 1920x960; check with a headless Chrome screenshot after layout changes.
- GitHub Actions are pinned to commit SHAs (Dependabot updates them).
- The deploy role trusts GitHub's immutable OIDC subject (`repo:OWNER@ID/REPO@ID:ref:refs/heads/main`);
  the role ARN is a repo secret so the AWS account ID stays masked in public logs.
- Assumed values (property tax, insurance) are labeled as placeholders in the UI; keep it that way.
