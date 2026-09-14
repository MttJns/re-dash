"""Build the static dashboard: fetch public sources, write per-region JSON and the site into dist/."""

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
from typing import Callable, Iterable, Optional

ROOT = Path(__file__).parent
CACHE = ROOT / ".cache"
SITE = ROOT / "site"

REDFIN_BASE = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker"
REDFIN_URL = f"{REDFIN_BASE}/redfin_metro_market_tracker.tsv000.gz"
REDFIN_COUNTY_URL = f"{REDFIN_BASE}/county_market_tracker.tsv000.gz"
ZILLOW_BASE = "https://files.zillowstatic.com/research/public_csvs"
ZILLOW_URLS = {
    "zhvi_entry": f"{ZILLOW_BASE}/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.0_0.33_sm_sa_month.csv",
    "zhvi_mid": f"{ZILLOW_BASE}/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zhvi_luxury": f"{ZILLOW_BASE}/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.67_1.0_sm_sa_month.csv",
    "zori": f"{ZILLOW_BASE}/zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
}
ZILLOW_COUNTY_URLS = {key: url.replace("/Metro_", "/County_") for key, url in ZILLOW_URLS.items()}
PMMS_URL = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
BPS_BASE = "https://www2.census.gov/econ/bps/CBSA%20(beginning%20Jan%202024)/"
BPS_COUNTY_BASE = "https://www2.census.gov/econ/bps/County/"
PEP_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-{vintage}/metro/totals/cbsa-est{vintage}-alldata.csv"
)
PEP_COUNTY_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-{vintage}/counties/totals/co-est{vintage}-alldata.csv"
)
CENSUS_URL = "https://api.census.gov/data/{year}/acs/{survey}?get=B19113_001E&for={geo}&key={key}"
CBSA_GEO = "metropolitan%20statistical%20area/micropolitan%20statistical%20area:{cbsa}"
COUNTY_GEO = "county:{county}&in=state:{state}"

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


def read_redfin(lines: Iterable[str], table_ids: set) -> dict:
    """Return {table_id: {"months", "updated", <field>: [...]}} for All Residential, not seasonally adjusted."""
    by_id = defaultdict(dict)
    for row in csv.DictReader(lines, delimiter="\t"):
        if (
            row["TABLE_ID"] in table_ids
            and row["PROPERTY_TYPE"] == "All Residential"
            and row["IS_SEASONALLY_ADJUSTED"] == "false"
        ):
            by_id[row["TABLE_ID"]][row["PERIOD_BEGIN"][:7]] = row
    out = {}
    for table_id, by_month in by_id.items():
        months = sorted(by_month)[-MONTHS:]
        series = {key: [num(by_month[m][col]) for m in months] for key, col in REDFIN_FIELDS.items()}
        series["pending_to_new"] = [
            round(p / n, 4) if p is not None and n else None
            for p, n in zip(series["pending_sales"], series["new_listings"])
        ]
        out[table_id] = {"months": months, "updated": by_month[months[-1]]["LAST_UPDATED"][:10], **series}
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


def read_bps_county(lines: Iterable[str], fips: set) -> dict:
    """Return {state+county fips: (single_family_units, multifamily_units)} from one Census BPS monthly county file."""
    out = {}
    for row in csv.reader(lines):
        if len(row) < 17 or not row[0].strip().isdigit():
            continue
        code = row[1].strip() + row[2].strip()
        if code in fips:
            # County files have an extra FIPS column, so units sit one column later than in CBSA files.
            out[code] = (num(row[7]), sum(num(row[i]) or 0 for i in (10, 13, 16)))
    return out


def pep_series(row: dict, vintage: int) -> dict:
    years = list(range(2021, vintage + 1))
    return {
        "vintage": vintage,
        "years": years,
        "population": [num(row[f"POPESTIMATE{y}"]) for y in years],
        "net": [num(row[f"NETMIG{y}"]) for y in years],
        "domestic": [num(row[f"DOMESTICMIG{y}"]) for y in years],
        "international": [num(row[f"INTERNATIONALMIG{y}"]) for y in years],
    }


def read_pep(lines: Iterable[str], cbsas: set, vintage: int) -> dict:
    """Return migration and population by year for metro rows; starts at 2021 since 2020 covers only April-July."""
    return {
        row["CBSA"]: pep_series(row, vintage)
        for row in csv.DictReader(lines)
        if row["CBSA"] in cbsas and row["LSAD"] == "Metropolitan Statistical Area"
    }


def read_pep_county(lines: Iterable[str], fips: set, vintage: int) -> dict:
    """Return migration and population by year for county rows, keyed by state+county fips."""
    return {
        row["STATE"] + row["COUNTY"]: pep_series(row, vintage)
        for row in csv.DictReader(lines)
        if row["SUMLEV"] == "050" and row["STATE"] + row["COUNTY"] in fips
    }


def fetch_pep(url: str, name: str) -> tuple:
    """Return (vintage, path) for the newest available Census population estimates file."""
    this_year = datetime.now(timezone.utc).year
    for vintage in range(this_year - 1, this_year - 4, -1):
        try:
            return vintage, fetch(url.format(vintage=vintage), name.format(vintage=vintage))
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise
    raise SystemExit("no Census population estimates file found")


def fetch_permits(base: str, pattern: str, reader: Callable, keys: set) -> dict:
    """Return {key: {"months", "single_family", "multifamily"}} from the last MONTHS monthly BPS files."""
    index = fetch(base, f"bps_index_{pattern[:2]}.html").read_text(errors="replace")
    names = sorted(set(re.findall(pattern, index)))[-MONTHS:]
    if not names:
        raise SystemExit(f"no Census building permit files found at {base}")
    permits = {key: {"months": [], "single_family": [], "multifamily": []} for key in keys}
    for name in names:
        with open(fetch(base + name, name), newline="", encoding="latin-1") as f:
            rows = reader(f, keys)
        yy, mm = re.search(r"(\d\d)(\d\d)c\.txt", name).groups()
        for key, series in permits.items():
            single_family, multifamily = rows.get(key, (None, None))
            series["months"].append(f"20{yy}-{mm}")
            series["single_family"].append(single_family)
            series["multifamily"].append(multifamily)
    return permits


def fetch_income(geo: str, key: Optional[str]) -> Optional[dict]:
    """Return the newest ACS median family income, preferring 1-year data and falling back to 5-year."""
    if not key:
        print("CENSUS_API_KEY not set; affordability index will be unavailable")
        return None
    this_year = datetime.now(timezone.utc).year
    for survey, label in (("acs1", "ACS 1-year"), ("acs5", "ACS 5-year")):
        for year in range(this_year - 1, this_year - 4, -1):
            try:
                url = CENSUS_URL.format(year=year, survey=survey, geo=geo, key=key)
                with urllib.request.urlopen(url, timeout=60) as r:
                    value = num(json.load(r)[1][0])
            except (urllib.error.URLError, json.JSONDecodeError, IndexError):
                continue
            if value:
                return {"year": year, "survey": label, "median_family_income": value}
    print(f"no ACS income found for {geo}")
    return None


def region_data(label: str, redfin: Optional[dict], zillow: dict, rid: str, permits: dict, migration: Optional[dict]) -> dict:
    """Check one region has every source and return its sourced series."""
    if redfin is None:
        raise SystemExit(f"no Redfin rows for {label}")
    missing = [k for k in zillow if rid not in zillow[k]]
    if missing:
        raise SystemExit(f"no Zillow rows for {label} in {missing}")
    if len({tuple(zillow[k][rid][0]) for k in zillow}) != 1:
        raise SystemExit(f"Zillow files disagree on months for {label}")
    if all(v is None for v in permits["single_family"]):
        raise SystemExit(f"no Census building permits for {label}")
    if migration is None:
        raise SystemExit(f"no Census population estimates for {label}")
    return {
        "redfin": redfin,
        "zillow": {"months": zillow["zhvi_mid"][rid][0], **{k: zillow[k][rid][1] for k in zillow}},
        "permits": permits,
        "migration": migration,
    }


def warn_if_counties_disagree(label: str, metro: dict, counties: list) -> None:
    """Print a warning when county permits or migration don't add up to the metro totals."""
    for key in ("single_family", "multifamily"):
        for i, month in enumerate(metro["permits"]["months"]):
            parts = [c["permits"][key][i] for c in counties]
            if metro["permits"][key][i] is not None and None not in parts and sum(parts) != metro["permits"][key][i]:
                print(f"warning: {label} {key} permits for {month}: counties sum to {sum(parts)}, metro {metro['permits'][key][i]}")
    for i, year in enumerate(metro["migration"]["years"]):
        parts = [c["migration"]["net"][i] for c in counties]
        if None not in parts and sum(parts) != metro["migration"]["net"][i]:
            print(f"warning: {label} net migration for {year}: counties sum to {sum(parts)}, metro {metro['migration']['net'][i]}")


def read_zillow_files(urls: dict, prefix: str, region_ids: set) -> dict:
    zillow = {}
    for key, url in urls.items():
        with open(fetch(url, f"{prefix}{key}.csv"), newline="") as f:
            zillow[key] = read_zillow(f, region_ids)
    return zillow


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
    counties = [c for m in metros for c in m.get("county", [])]
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    census_key = os.environ.get("CENSUS_API_KEY")

    with gzip.open(fetch(REDFIN_URL, "redfin_metro.tsv.gz"), "rt", newline="") as f:
        redfin = read_redfin(f, {m["cbsa"] for m in metros})
    with gzip.open(fetch(REDFIN_COUNTY_URL, "redfin_county.tsv.gz"), "rt", newline="") as f:
        redfin_county = read_redfin(f, {c["redfin_table_id"] for c in counties})
    zillow = read_zillow_files(ZILLOW_URLS, "", {m["zillow_region_id"] for m in metros})
    zillow_county = read_zillow_files(ZILLOW_COUNTY_URLS, "co_", {c["zillow_region_id"] for c in counties})
    with open(fetch(PMMS_URL, "pmms_history.csv"), newline="") as f:
        weeks, mortgage30 = read_pmms(f)

    cbsas = {m["cbsa"] for m in metros}
    fips = {c["fips"] for c in counties}
    permits = fetch_permits(BPS_BASE, r"cbsa\d{4}c\.txt", read_bps, cbsas)
    permits_county = fetch_permits(BPS_COUNTY_BASE, r"co\d{4}c\.txt", read_bps_county, fips)

    vintage, pep_path = fetch_pep(PEP_URL, "cbsa-est{vintage}.csv")
    with open(pep_path, newline="", encoding="latin-1") as f:
        migration = read_pep(f, cbsas, vintage)
    vintage, pep_path = fetch_pep(PEP_COUNTY_URL, "co-est{vintage}.csv")
    with open(pep_path, newline="", encoding="latin-1") as f:
        migration_county = read_pep_county(f, fips, vintage)

    if out.exists():
        shutil.rmtree(out)
    data_dir = out / "data" / version
    data_dir.mkdir(parents=True)

    def write(path: str, doc: dict) -> None:
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        (out / path).write_text(json.dumps(doc, separators=(",", ":")))
        print(f"wrote {path}")

    entries = []
    for m in metros:
        base = {
            "metro": {"id": m["id"], "name": m["name"], "cbsa": m["cbsa"]},
            "built_at": version,
            "assumptions": {
                **config["assumptions"],
                "property_tax_rate": m["property_tax_rate"],
                "insurance_annual": m["insurance_annual"],
            },
            "rates": {"weeks": weeks, "mortgage30": mortgage30},
        }
        metro_data = region_data(
            f"{m['id']} (cbsa {m['cbsa']})", redfin.get(m["cbsa"]), zillow, m["zillow_region_id"],
            permits[m["cbsa"]], migration.get(m["cbsa"]),
        )
        path = f"data/{version}/{m['id']}.json"
        write(path, {**base, **metro_data, "income": fetch_income(CBSA_GEO.format(cbsa=m["cbsa"]), census_key)})
        entry = {"id": m["id"], "name": m["name"], "path": path}

        regions, county_data = [], []
        for c in m.get("county", []):
            data = region_data(
                f"{m['id']}/{c['id']} (fips {c['fips']})", redfin_county.get(c["redfin_table_id"]), zillow_county,
                c["zillow_region_id"], permits_county[c["fips"]], migration_county.get(c["fips"]),
            )
            county_data.append(data)
            geo = COUNTY_GEO.format(state=c["fips"][:2], county=c["fips"][2:])
            path = f"data/{version}/{m['id']}/{c['id']}.json"
            region = {"id": c["id"], "name": c["name"], "fips": c["fips"]}
            write(path, {**base, "region": region, **data, "income": fetch_income(geo, census_key)})
            regions.append({"id": c["id"], "name": c["name"], "path": path})
        if regions:
            warn_if_counties_disagree(m["id"], metro_data, county_data)
            if m.get("default_region") not in {r["id"] for r in regions}:
                raise SystemExit(f"default_region for {m['id']} must be one of its counties")
            entry.update(default_region=m["default_region"], regions=regions)
        entries.append(entry)

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
