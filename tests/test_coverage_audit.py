import datetime
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "audit_coverage", Path(__file__).resolve().parents[1] / "scripts" / "audit_coverage.py")
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)


class CoverageAuditTests(unittest.TestCase):
    def sale(self, identity, lat=37.8, lng=-122.3):
        return dict(id=identity, address=f"{identity} Example Street, Example City, CA 94000",
                    city="Example City", lat=lat, lng=lng, price=2000000, sqft=2500,
                    beds=3, sold_date="2026-10-01", home_type="singleFamily",
                    source_url=f"https://www.zillow.com/homedetails/{identity}_zpid/")

    def test_grid_roundtrip_and_negative_longitude(self):
        for lat, lng in [(37.8, -122.3), (36.81, -123.59), (38.89, -121.16)]:
            key = coverage.cell_key(lat, lng)
            center = coverage.cell_center(key)
            self.assertEqual(coverage.cell_key(*center), key)
            self.assertLess(abs(center[0] - lat) * coverage.KM_PER_LAT_DEGREE, .251)
            self.assertLess(abs(center[1] - lng) * coverage.X_SCALE, .251)

    def test_only_observed_cells_and_home_cannot_validate_itself(self):
        result = coverage.audit([self.sale("1")], datetime.date(2026, 10, 31))
        self.assertEqual(result["summary"]["occupied_cells"], 1)
        self.assertEqual(result["summary"]["eligible_homes"], 1)
        self.assertEqual(result["cells"][0]["comparables"], 0)
        self.assertEqual(result["cells"][0]["status"], "insufficient_data")
        self.assertEqual(result["cells"][0]["lat"], 37.8)

    def test_assessor_sees_held_out_comparables(self):
        observed = []
        def assessor(result):
            observed.append(result["comparable_count"])
            return {"target_met": False, "failed_gates": ["example_gate"]}
        rows = [self.sale(str(i), lat=37.8 + i * .00001) for i in range(1, 5)]
        result = coverage.audit(rows, datetime.date(2026, 10, 31), assessor=assessor)
        self.assertEqual(observed, [3])
        self.assertEqual(result["summary"]["failed_gate_counts"], {"example_gate": 1})
        self.assertEqual(result["zips"]["94000"]["eligible_homes"], 4)

    def test_invalid_spacing(self):
        for spacing in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                coverage.cell_key(37.8, -122.3, spacing)

    def test_reference_audit_keeps_probe_fixed_after_more_sales(self):
        before = coverage.audit([self.sale("1")], datetime.date(2026, 10, 31))
        lat, lng = coverage.cell_center(coverage.cell_key(37.8, -122.3))
        observed = []
        def assessor(result):
            observed.append((result["subject"]["lat"],
                             [row["id"] for row in result["comparables"]]))
            return {}
        after = coverage.audit([self.sale("1"), self.sale("2", lat=lat, lng=lng)],
                               datetime.date(2026, 10, 31), assessor=assessor,
                               reference_cells=before["cells"])
        self.assertEqual(observed, [(37.8, ["2"])])
        self.assertEqual(after["reference_cells_reused"], 1)
        self.assertEqual(after["cells"][0]["probe_id"], "1")

    def test_reference_holdout_follows_home_when_source_id_changes(self):
        before = coverage.audit([self.sale("1")], datetime.date(2026, 10, 31))
        replacement = dict(self.sale("1"), id="other-provider-id", source_id="1234",
                           source_url="https://www.redfin.com/CA/Example-City/1-Example-St-94000/home/1234")
        after = coverage.audit([replacement], datetime.date(2026, 10, 31),
                               reference_cells=before["cells"])
        self.assertEqual(after["cells"][0]["comparables"], 0)
        self.assertEqual(after["reference_comparison"]["after_same_cells"]["evidence_tier_counts"],
                         {"insufficient": 1})


if __name__ == "__main__":
    unittest.main()
