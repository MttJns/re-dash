import csv
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build import (  # noqa: E402
    MONTHS,
    REDFIN_FIELDS,
    read_bps,
    read_bps_county,
    read_pep,
    read_pep_county,
    read_pmms,
    read_redfin,
    read_zillow,
)

BASE = {
    "TABLE_ID": "12420",
    "IS_SEASONALLY_ADJUSTED": "false",
    "PROPERTY_TYPE": "All Residential",
    "LAST_UPDATED": "2026-06-02 14:33:24.470 Z",
}


def redfin_tsv(rows: list) -> io.StringIO:
    fields = ["PERIOD_BEGIN", *BASE, *REDFIN_FIELDS.values()]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fields, delimiter="\t", restval="", quoting=csv.QUOTE_NONNUMERIC)
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return buf


class ReadRedfinTest(unittest.TestCase):
    def test_filters_to_all_residential_unadjusted_for_requested_cbsa(self):
        rows = [
            {**BASE, "PERIOD_BEGIN": "2026-05-01", "INVENTORY": "15220", "PENDING_SALES": "3557", "NEW_LISTINGS": "4762"},
            {**BASE, "PERIOD_BEGIN": "2026-04-01", "INVENTORY": "14741", "PENDING_SALES": "3815", "NEW_LISTINGS": "0"},
            {**BASE, "PERIOD_BEGIN": "2026-05-01", "IS_SEASONALLY_ADJUSTED": "true", "INVENTORY": "1"},
            {**BASE, "PERIOD_BEGIN": "2026-05-01", "PROPERTY_TYPE": "Condo/Co-op", "INVENTORY": "2"},
            {**BASE, "PERIOD_BEGIN": "2026-05-01", "TABLE_ID": "19100", "INVENTORY": "3"},
        ]
        out = read_redfin(redfin_tsv(rows), {"12420"})
        self.assertEqual(list(out), ["12420"])
        austin = out["12420"]
        self.assertEqual(austin["months"], ["2026-04", "2026-05"])
        self.assertEqual(austin["inventory"], [14741.0, 15220.0])
        self.assertEqual(austin["median_dom"], [None, None])
        self.assertEqual(austin["pending_to_new"], [None, round(3557 / 4762, 4)])
        self.assertEqual(austin["updated"], "2026-06-02")

    def test_keeps_only_most_recent_window(self):
        rows = [
            {**BASE, "PERIOD_BEGIN": f"{2020 + i // 12}-{i % 12 + 1:02d}-01", "INVENTORY": str(i)}
            for i in range(MONTHS + 3)
        ]
        austin = read_redfin(redfin_tsv(rows), {"12420"})["12420"]
        self.assertEqual(len(austin["months"]), MONTHS)
        self.assertEqual(austin["inventory"][-1], float(MONTHS + 2))


class ReadZillowTest(unittest.TestCase):
    def test_reads_quoted_region_and_blank_values(self):
        text = (
            "RegionID,SizeRank,RegionName,RegionType,StateName,2026-06-30,2026-07-31\n"
            '394355,29,"Austin, TX",msa,TX,1641.69,\n'
            '394913,1,"New York, NY",msa,NY,3300.1,3310.2\n'
        )
        out = read_zillow(io.StringIO(text), {"394355"})
        self.assertEqual(out, {"394355": (["2026-06", "2026-07"], [1641.69, None])})


class ReadPmmsTest(unittest.TestCase):
    def test_parses_us_dates_and_blank_rates(self):
        text = "date,pmms30,pmms30p,pmms15\n9/3/2026,6.71,,6.04\n9/10/2026, ,,6.09\n"
        self.assertEqual(read_pmms(io.StringIO(text)), (["2026-09-03", "2026-09-10"], [6.71, None]))


class ReadBpsTest(unittest.TestCase):
    def test_sums_multifamily_units_for_requested_cbsa(self):
        text = (
            "Survey,CSA,CBSA,HHEADER,CBSA,,1-unit,,,2-units,,,3-4 units,,,5+ units,,,1-unit rep\n"
            "Date,Code,Code,,Name,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs\n"
            " \n"
            "202607,999,12420,4,Austin-Round Rock-San Marcos  TX ,1419,1419,378277,5,10,1183,5,19,2453,23,1157,10484,1064\n"
            "202607,206,19100,4,Dallas-Fort Worth-Arlington  TX ,1,2,3,4,5,6,7,8,9,10,11,12,13\n"
        )
        self.assertEqual(read_bps(io.StringIO(text), {"12420"}), {"12420": (1419.0, 1186.0)})


class ReadPepTest(unittest.TestCase):
    def test_reads_metro_row_from_2021(self):
        fields = ("POPESTIMATE", "NETMIG", "DOMESTICMIG", "INTERNATIONALMIG")
        columns = ["CBSA", "LSAD", *[f"{field}{year}" for field in fields for year in range(2020, 2026)]]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        # Each value is its column offset plus a base, so assertions can locate it.
        writer.writerow(["12420", "Metropolitan Statistical Area", *[str(i) for i in range(len(columns) - 2)]])
        writer.writerow(["12420", "County or equivalent", *[str(1000 + i) for i in range(len(columns) - 2)]])
        buf.seek(0)
        austin = read_pep(buf, {"12420"}, 2025)["12420"]
        self.assertEqual(austin["years"], [2021, 2022, 2023, 2024, 2025])
        self.assertEqual(austin["population"][0], 1.0)  # POPESTIMATE2021
        self.assertEqual(austin["net"][-1], 11.0)  # NETMIG2025
        self.assertEqual(austin["domestic"][0], 13.0)  # DOMESTICMIG2021
        self.assertEqual(austin["international"][-1], 23.0)  # INTERNATIONALMIG2025
        self.assertEqual(austin["vintage"], 2025)

    def test_reads_county_rows_by_fips(self):
        fields = ("POPESTIMATE", "NETMIG", "DOMESTICMIG", "INTERNATIONALMIG")
        columns = ["SUMLEV", "STATE", "COUNTY", *[f"{field}{year}" for field in fields for year in range(2020, 2026)]]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        writer.writerow(["040", "48", "000", *["9"] * (len(columns) - 3)])
        writer.writerow(["050", "48", "453", *[str(i) for i in range(len(columns) - 3)]])
        writer.writerow(["050", "48", "491", *["7"] * (len(columns) - 3)])
        buf.seek(0)
        out = read_pep_county(buf, {"48453"}, 2025)
        self.assertEqual(list(out), ["48453"])
        self.assertEqual(out["48453"]["net"][-1], 11.0)  # NETMIG2025


class ReadBpsCountyTest(unittest.TestCase):
    def test_reads_units_one_column_later_than_cbsa_files(self):
        text = (
            "Survey,FIPS,FIPS,Region,Division,County,,1-unit,,,2-units,,,3-4 units,,,5+ units,,,1-unit rep\n"
            "Date,State,County,Code,Code,Name,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs\n"
            " \n"
            "202607,48,453,3,7,Travis County    ,548,548,114261896,4,8,945429,5,19,2452718,23,1157,10483639,497\n"
            "202607,48,055,3,7,Caldwell County  ,33,33,8114873,0,0,0,0,0,0,0,0,0,0\n"
            "202607,01,453,3,6,Not Texas        ,1,1,1,1,1,1,1,1,1,1,1,1,1\n"
        )
        self.assertEqual(read_bps_county(io.StringIO(text), {"48453"}), {"48453": (548.0, 1184.0)})


if __name__ == "__main__":
    unittest.main()
