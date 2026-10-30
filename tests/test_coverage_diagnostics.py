import contextlib
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.coverage_diagnostics import main, summarize


AS_OF = date(2026, 10, 30)


def sale(identity, **overrides):
    row = {"id": str(identity), "address": f"{identity} Private Example Street",
           "city": "Sunnyvale", "lat": 37.37, "lng": -122.04,
           "price": 2_000_000, "sqft": 2500, "beds": 3,
           "sold_date": "2026-10-20", "home_type": "singleFamily",
           "source_url": f"https://www.zillow.com/homedetails/{identity}_zpid/"}
    row.update(overrides)
    return row


class CoverageDiagnosticsTests(unittest.TestCase):
    def test_current_eligible_city_counts_include_requested_zeros(self):
        rows = [sale(1), sale(1), sale(2, city="Santa Clara"),
                sale(3, sold_date="2024-10-29"), sale(4, home_type="condo")]
        report = summarize({"sales": rows}, as_of=AS_OF, cities=["Mountain View"])
        self.assertEqual(report["city_counts"], {"Mountain View": 0, "Santa Clara": 1, "Sunnyvale": 1})
        self.assertEqual(report["current_eligible_count"], 2)
        self.assertEqual(report["eligibility"]["excluded"],
                         {"outside_date_window": 1, "not_single_family": 1, "duplicate_home": 1})

    def test_completed_searches_preserve_known_gap_and_omit_private_fields(self):
        snapshot = {"sales": [sale(1)], "coverage": {
            "complete": False, "counties_completed": 9, "counties_total": 9,
            "county_searches_finished": True, "note": "secret-provider-input",
            "verified_source_gaps": [{"county": "Santa Clara County, CA",
                "source_coverage": "partial", "unreachable_count": 1616,
                "run_id": "private-run-id", "provider_status_message": "secret-provider-input"}]}}
        report = summarize(snapshot, as_of=AS_OF)
        self.assertTrue(report["coverage"]["county_searches_finished"])
        self.assertFalse(report["coverage"]["reported_complete"])
        self.assertIn("1,616 unreachable", report["warnings"][0])
        output = json.dumps(report)
        for private in ["Private Example Street", "private-run-id", "secret-provider-input", "zillow.com"]:
            self.assertNotIn(private, output)

    def test_absent_or_malformed_metadata_never_implies_complete_coverage(self):
        report = summarize({"sales": [], "coverage": {"verified_source_gaps": [None,
            {"county": "123 Private Street", "unreachable_count": True, "source_coverage": "partial"}]}}, as_of=AS_OF)
        self.assertIsNone(report["coverage"]["reported_complete"])
        self.assertEqual(report["coverage"]["verified_source_gaps"], [
            {"county": "Unknown", "unreachable_count": None, "source_coverage": "partial"}])
        self.assertNotIn("123 Private Street", json.dumps(report))

    def test_cli_does_not_write_dataset_or_report_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "sales.json"
            path.parent.mkdir()
            original = json.dumps({"sales": [sale(1)]}).encode()
            path.write_bytes(original)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["--root", directory, "--as-of", AS_OF.isoformat(), "--city", "Mountain View"])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertTrue(report["coherent_reads"])
            self.assertEqual(report["city_counts"]["Mountain View"], 0)
            self.assertNotIn(directory, output.getvalue())


if __name__ == "__main__":
    unittest.main()
