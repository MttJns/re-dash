"""Build the static dashboard: fetch public sources, write per-metro JSON and the site into dist/."""

import argparse
import csv
import gzip
import hashlib
import json
import os
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
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US"
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
    with urllib.request.urlopen(request, timeout=600) as response, open(tmp, "wb") as f:
        shutil.copyfileobj(response, f)
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


def read_fred(lines: Iterable[str]) -> tuple:
    reader = csv.reader(lines)
    next(reader)
    rows = [(day, num(value)) for day, value in reader][-WEEKS:]
    return [day for day, _ in rows], [value for _, value in rows]


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
    with open(fetch(FRED_URL, "MORTGAGE30US.csv"), newline="") as f:
        weeks, mortgage30 = read_fred(f)

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
