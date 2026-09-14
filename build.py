"""Build the static dashboard: fetch public sources, write per-metro JSON and the site into dist/."""

import argparse
import csv
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import time
import tomllib
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).parent
CACHE = ROOT / ".cache"
SITE = ROOT / "site"

REDFIN_URL = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/redfin_metro_market_tracker.tsv000.gz"
ZILLOW_BASE = "https://files.zillowstatic.com/research/public_csvs"
ZILLOW_URLS = {
    "zhvi_entry": f"{ZILLOW_BASE}/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.0_0.33_sm_sa_month.csv",
    "zhvi_mid": f"{ZILLOW_BASE}/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zhvi_luxury": f"{ZILLOW_BASE}/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.67_1.0_sm_sa_month.csv",
    "zori": f"{ZILLOW_BASE}/zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
}
PMMS_URL = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
BPS_BASE = "https://www2.census.gov/econ/bps/CBSA%20(beginning%20Jan%202024)/"
PEP_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-{vintage}/metro/totals/cbsa-est{vintage}-alldata.csv"
)
CENSUS_URL = (
    "https://api.census.gov/data/{year}/acs/acs1?get=B19113_001E"
    "&for=metropolitan%20statistical%20area/micropolitan%20statistical%20area:{cbsa}&key={key}"
)

MONTHS = 25  # current month plus two years of history
WEEKS = 105

REDFIN_FIELDS = {
    "median_sale_price": "MEDIAN_SALE_PRICE",
    "inventory": "INVENTORY",
    "months_of_supply": "MONTHS_OF_SUPPLY",
    "median_dom": "MEDIAN_DOM",
    "sale_to_list": "AVG_SALE_TO_LIST",
    "sold_above_list": "SOLD_ABOVE_LIST",
    "price_drops": "PRICE_DROPS",
    "median_ppsf": "MEDIAN_PPSF",
    "median_list_ppsf": "MEDIAN_LIST_PPSF",
    "homes_sold": "HOMES_SOLD",
    "pending_sales": "PENDING_SALES",
    "new_listings": "NEW_LISTINGS",
}


def num(text: str) -> Optional[float]:
    try:
        return round(float(text), 4)
    except ValueError:
        return None


def fetch(url: str, name: str, max_age_hours: float = 12) -> Path:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / name
    if path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        return path
    print(f"fetching {url}")
    tmp = path.with_name(path.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "re-dash"})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as f:
                shutil.copyfileobj(response, f)
            break
        except (OSError, http.client.HTTPException) as err:
            if attempt == 3 or (isinstance(err, urllib.error.HTTPError) and err.code < 500):
                raise
            print(f"  attempt {attempt} failed ({err}); retrying")
            time.sleep(5 * attempt)
    tmp.replace(path)
    return path


def read_redfin(lines: Iterable[str], cbsas: set) -> dict:
    """Return {cbsa: {"months", "updated", <field>: [...]}} for All Residential, not seasonally adjusted."""
    by_cbsa = defaultdict(dict)
    for row in csv.DictReader(lines, delimiter="\t"):
        if (
            row["TABLE_ID"] in cbsas
            and row["PROPERTY_TYPE"] == "All Residential"
            and row["IS_SEASONALLY_ADJUSTED"] == "false"
        ):
            by_cbsa[row["TABLE_ID"]][row["PERIOD_BEGIN"][:7]] = row
    out = {}
    for cbsa, by_month in by_cbsa.items():
        months = sorted(by_month)[-MONTHS:]
        series = {key: [num(by_month[m][col]) for m in months] for key, col in REDFIN_FIELDS.items()}
        series["pending_to_new"] = [
            round(p / n, 4) if p is not None and n else None
            for p, n in zip(series["pending_sales"], series["new_listings"])
        ]
        out[cbsa] = {"months": months, "updated": by_month[months[-1]]["LAST_UPDATED"][:10], **series}
    return out


def read_zillow(lines: Iterable[str], region_ids: set) -> dict:
    """Return {region_id: (months, values)} for the last MONTHS columns of a Zillow wide CSV."""
    reader = csv.reader(lines)
    header = next(reader)
    date_cols = [i for i, h in enumerate(header) if h[:2] in ("19", "20")][-MONTHS:]
    out = {}
    for row in reader:
        if row[0] in region_ids:
            out[row[0]] = ([header[i][:7] for i in date_cols], [num(row[i]) for i in date_cols])
    return out


def read_pmms(lines: Iterable[str]) -> tuple:
    """Return (weeks, 30-yr rates) from Freddie Mac's PMMS history CSV, whose dates are M/D/YYYY."""
    rows = []
    for row in csv.DictReader(lines):
        month, day, year = row["date"].split("/")
        rows.append((f"{year}-{int(month):02d}-{int(day):02d}", num(row["pmms30"])))
    rows = rows[-WEEKS:]
    return [day for day, _ in rows], [value for _, value in rows]


def read_bps(lines: Iterable[str], cbsas: set) -> dict:
    """Return {cbsa: (single_family_units, multifamily_units)} from one Census BPS monthly CBSA file."""
    out = {}
    for row in csv.reader(lines):
        if len(row) < 16 or not row[0].strip().isdigit() or row[2].strip() not in cbsas:
            continue
        # Units columns for 1-unit, 2-unit, 3-4 unit, and 5+ unit structures.
        multifamily = sum(num(row[i]) or 0 for i in (9, 12, 15))
        out[row[2].strip()] = (num(row[6]), multifamily)
    return out


def read_pep(lines: Iterable[str], cbsas: set, vintage: int) -> dict:
    """Return migration and population by year for metro rows; starts at 2021 since 2020 covers only April-July."""
    years = list(range(2021, vintage + 1))
    out = {}
    for row in csv.DictReader(lines):
        if row["CBSA"] in cbsas and row["LSAD"] == "Metropolitan Statistical Area":
            out[row["CBSA"]] = {
                "vintage": vintage,
                "years": years,
                "population": [num(row[f"POPESTIMATE{y}"]) for y in years],
                "net": [num(row[f"NETMIG{y}"]) for y in years],
                "domestic": [num(row[f"DOMESTICMIG{y}"]) for y in years],
                "international": [num(row[f"INTERNATIONALMIG{y}"]) for y in years],
            }
    return out


def fetch_pep() -> tuple:
    """Return (vintage, path) for the newest available Census metro population estimates file."""
    this_year = datetime.now(timezone.utc).year
    for vintage in range(this_year - 1, this_year - 4, -1):
        try:
            return vintage, fetch(PEP_URL.format(vintage=vintage), f"cbsa-est{vintage}.csv")
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise
    raise SystemExit("no Census population estimates file found")


def fetch_income(cbsa: str, key: Optional[str]) -> Optional[dict]:
    if not key:
        print("CENSUS_API_KEY not set; affordability index will be unavailable")
        return None
    this_year = datetime.now(timezone.utc).year
    for year in range(this_year - 1, this_year - 4, -1):
        try:
            with urllib.request.urlopen(CENSUS_URL.format(year=year, cbsa=cbsa, key=key), timeout=60) as r:
                value = num(json.load(r)[1][0])
        except (urllib.error.URLError, json.JSONDecodeError, IndexError):
            continue
        if value:
            return {"year": year, "median_family_income": value}
    print(f"no ACS income found for cbsa {cbsa}")
    return None


def copy_site(out: Path) -> None:
    html = (SITE / "index.html").read_text()
    for name in ("app.css", "app.js"):
        body = (SITE / name).read_bytes()
        stem, ext = name.split(".")
        hashed = f"{stem}.{hashlib.sha256(body).hexdigest()[:10]}.{ext}"
        (out / hashed).write_bytes(body)
        html = html.replace(f'"{name}"', f'"{hashed}"')
    (out / "index.html").write_text(html)
    for name in ("favicon.ico", "favicon-32.png", "apple-touch-icon.png"):
        shutil.copyfile(SITE / name, out / name)


def build(config_path: Path, out: Path) -> None:
    config = tomllib.loads(config_path.read_text())
    metros = config["metro"]
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with gzip.open(fetch(REDFIN_URL, "redfin_metro.tsv.gz"), "rt", newline="") as f:
        redfin = read_redfin(f, {m["cbsa"] for m in metros})
    zillow = {}
    for key, url in ZILLOW_URLS.items():
        with open(fetch(url, f"{key}.csv"), newline="") as f:
            zillow[key] = read_zillow(f, {m["zillow_region_id"] for m in metros})
    with open(fetch(PMMS_URL, "pmms_history.csv"), newline="") as f:
        weeks, mortgage30 = read_pmms(f)

    cbsas = {m["cbsa"] for m in metros}
    bps_index = fetch(BPS_BASE, "bps_index.html").read_text(errors="replace")
    bps_files = sorted(set(re.findall(r"cbsa\d{4}c\.txt", bps_index)))[-MONTHS:]
    if not bps_files:
        raise SystemExit("no Census building permit files found")
    permits = {cbsa: {"months": [], "single_family": [], "multifamily": []} for cbsa in cbsas}
    for name in bps_files:
        with open(fetch(BPS_BASE + name, name), newline="", encoding="latin-1") as f:
            rows = read_bps(f, cbsas)
        for cbsa, series in permits.items():
            single_family, multifamily = rows.get(cbsa, (None, None))
            series["months"].append(f"20{name[4:6]}-{name[6:8]}")
            series["single_family"].append(single_family)
            series["multifamily"].append(multifamily)

    vintage, pep_path = fetch_pep()
    with open(pep_path, newline="", encoding="latin-1") as f:
        migration = read_pep(f, cbsas, vintage)

    if out.exists():
        shutil.rmtree(out)
    data_dir = out / "data" / version
    data_dir.mkdir(parents=True)

    entries = []
    for m in metros:
        rid = m["zillow_region_id"]
        if m["cbsa"] not in redfin:
            raise SystemExit(f"no Redfin rows for {m['id']} (cbsa {m['cbsa']})")
        missing = [k for k in ZILLOW_URLS if rid not in zillow[k]]
        if missing:
            raise SystemExit(f"no Zillow rows for {m['id']} in {missing}")
        if len({tuple(zillow[k][rid][0]) for k in ZILLOW_URLS}) != 1:
            raise SystemExit(f"Zillow files disagree on months for {m['id']}")
        if all(v is None for v in permits[m["cbsa"]]["single_family"]):
            raise SystemExit(f"no Census building permits for {m['id']} (cbsa {m['cbsa']})")
        if m["cbsa"] not in migration:
            raise SystemExit(f"no Census population estimates for {m['id']} (cbsa {m['cbsa']})")

        doc = {
            "metro": {"id": m["id"], "name": m["name"], "cbsa": m["cbsa"]},
            "built_at": version,
            "assumptions": {
                **config["assumptions"],
                "property_tax_rate": m["property_tax_rate"],
                "insurance_annual": m["insurance_annual"],
            },
            "redfin": redfin[m["cbsa"]],
            "zillow": {"months": zillow["zhvi_mid"][rid][0], **{k: zillow[k][rid][1] for k in ZILLOW_URLS}},
            "rates": {"weeks": weeks, "mortgage30": mortgage30},
            "permits": permits[m["cbsa"]],
            "migration": migration[m["cbsa"]],
            "income": fetch_income(m["cbsa"], os.environ.get("CENSUS_API_KEY")),
        }
        path = f"data/{version}/{m['id']}.json"
        (out / path).write_text(json.dumps(doc, separators=(",", ":")))
        entries.append({"id": m["id"], "name": m["name"], "path": path})
        print(f"wrote {path}")

    manifest = {"built_at": version, "metros": entries}
    (out / "data" / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))
    copy_site(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "metros.toml")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(args.config, args.out)


if __name__ == "__main__":
    main()
