import math
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from model import bedroom_weight, estimate, haversine_km, merge_sales, model_description, prepare_sales, time_weight, two_year_cutoff

AS_OF = date(2026, 10, 29)


def sale(index=1, *, age=10, ppsf=800, sqft=2500, beds=3, **extra):
    row = {
        "id": str(index), "address": f"{index} Test St", "city": "Oakland",
        "lat": 37.8 + index * 0.00001, "lng": -122.25,
        "price": ppsf * sqft, "sqft": sqft, "beds": beds,
        "sold_date": (AS_OF - timedelta(days=age)).isoformat(),
        "source_url": f"https://www.zillow.com/homedetails/{index}_zpid/",
        "home_type": "singleFamily",
    }
    row.update(extra)
    return row


class SaleEligibilityTests(unittest.TestCase):
    def test_every_time_bucket_boundary(self):
        for days, expected in ((0, 1), (30, 1), (31, .8), (90, .8), (91, .55),
                               (183, .55), (184, .3), (365, .3), (366, .15),
                               (730, .15), (731, 0), (-1, 0)):
            with self.subTest(days=days):
                self.assertEqual(time_weight(AS_OF - timedelta(days=days), AS_OF), expected)

    def test_two_calendar_years_handles_leap_days(self):
        self.assertEqual(two_year_cutoff(date(2024, 2, 29)), date(2022, 2, 28))
        self.assertEqual(time_weight(date(2022, 2, 28), date(2024, 2, 29)), .15)
        self.assertEqual(time_weight(date(2022, 2, 27), date(2024, 2, 29)), 0)
        self.assertEqual(time_weight(date(2023, 1, 1), date(2025, 1, 1)), .15)

    def test_latest_sale_per_home_retained(self):
        old = sale(1, age=200, ppsf=600)
        new = sale(1, age=5, ppsf=850)
        alternate_id = dict(new, id="second-id", address="1 Test Street")
        cleaned, stats = prepare_sales([old, new, alternate_id], AS_OF)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["price_per_sqft"], 850)
        self.assertEqual(stats["excluded"]["duplicate_home"], 2)

    def test_rejects_attached_non_sold_old_and_unattributed_records(self):
        records = [sale(1, home_type="condo"), sale(2, is_attached=True),
                   sale(3, status="for_sale"), sale(4, age=731),
                   sale(5, source_url="https://example.com/zillow.com"),
                   sale(6, lat=float("nan")), sale(7, sqft=1999), sale(8, beds=1),
                   sale(9, source_url="https://zillow.com.attacker.example/"),
                   sale(10, price=float("inf")), sale(11, sqft=2000, beds=2), sale(12, sqft=3000)]
        cleaned, stats = prepare_sales(records, AS_OF)
        self.assertEqual({row["id"] for row in cleaned}, {"11", "12"})
        self.assertEqual(sum(stats["excluded"].values()), 10)


class PrecisionUpgradeTests(unittest.TestCase):
    def test_exact_history_upgrades_only_explicit_rounded_same_property_sale(self):
        rounded = sale(price=2_630_000, price_evidence="Apify Zillow listingSoldPrice display amount; may be rounded")
        exact = sale(price=2_633_000, price_evidence="Apify Zillow listingPriceHistory; exact Sold event price matching dateSold")
        merged, counts = merge_sales([rounded], [exact], AS_OF)
        self.assertEqual(merged[0]["price"], 2_633_000)
        self.assertEqual(counts["updated"], 1)
        for saved, incoming in ((sale(price=2_630_000), exact),
                                (exact, rounded),
                                (rounded, dict(exact, beds=4)),
                                (rounded, dict(exact, id="99", source_url="https://www.zillow.com/homedetails/99_zpid/"))):
            merged, counts = merge_sales([saved], [incoming], AS_OF)
            self.assertEqual(merged[0]["price"], saved["price"])
            self.assertEqual(counts["updated"], 0)
            self.assertEqual(counts["conflicts"], 1)


class EstimationTests(unittest.TestCase):
    def test_empty_and_geographically_distant_data_do_not_extrapolate(self):
        self.assertIsNone(estimate([], 37.8, -122.25, as_of=AS_OF)["estimate_ppsf"])
        far = [sale(i, lat=38.0) for i in range(1, 12)]
        self.assertEqual(estimate(far, 37.8, -122.25, as_of=AS_OF)["comparable_count"], 0)

    def test_minimum_raw_and_effective_counts(self):
        self.assertIsNone(estimate([sale(1), sale(2)], 37.8, -122.25, as_of=AS_OF)["estimate_ppsf"])
        dominated = [sale(1, age=2), sale(2, age=500), sale(3, age=500)]
        result = estimate(dominated, 37.8, -122.25, as_of=AS_OF)
        self.assertEqual(result["comparable_count"], 3)
        self.assertLess(result["effective_sample_count"], 2)
        self.assertIsNone(result["estimate_ppsf"])

    def test_identical_prices_have_correct_scale_and_total(self):
        result = estimate([sale(i) for i in range(1, 12)], 37.8, -122.25, sqft=2400, as_of=AS_OF)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["estimate_ppsf"], 800)
        self.assertEqual(result["estimated_price"], 1_920_000)
        self.assertEqual(result["range_ppsf"], {"low": 800, "high": 800})
        self.assertEqual(result["radius_km"], .75)
        self.assertAlmostEqual(sum(comp["normalized_weight"] for comp in result["comparables"]), 1)

    def test_single_extreme_price_cannot_drag_local_estimate(self):
        records = [sale(i) for i in range(1, 10)] + [sale(10, ppsf=8000)]
        result = estimate(records, 37.8, -122.25, as_of=AS_OF)
        self.assertLess(result["estimate_ppsf"], 825)
        self.assertGreaterEqual(result["outlier_count"], 1)

    def test_recency_and_two_bedroom_weights_are_inspectable(self):
        records = [sale(1, age=3, lat=37.8), sale(2, age=100, lat=37.8),
                   sale(3, age=3, beds=2, lat=37.8), sale(4, age=3, lat=37.8)]
        result = estimate(records, 37.8, -122.25, as_of=AS_OF)
        by_id = {row["id"]: row for row in result["comparables"]}
        self.assertEqual(by_id["2"]["weight_components"]["recency"], .55)
        self.assertAlmostEqual(by_id["3"]["weight"] / by_id["1"]["weight"], .75)
        self.assertEqual(result["status"], "limited")

    def test_bedroom_matching_buckets_and_two_bedroom_exception(self):
        expected = {2: [1, .9, .81, .729, .729],
                    3: [.75, 1, .9, .81, .81],
                    4: [.75, .9, 1, .9, .9],
                    5: [.75, .81, .9, 1, 1],
                    10: [.75, .81, .9, 1, 1]}
        for subject, weights in expected.items():
            for comp, weight in zip((2, 3, 4, 5, 8), weights):
                with self.subTest(subject=subject, comp=comp):
                    self.assertAlmostEqual(bedroom_weight(comp, subject), weight)

    def test_selector_changes_comparison_weights_and_estimate_without_price_premium(self):
        # Higher-bedroom comps happen to have lower prices here. Matching them
        # should lower the estimate, not apply an invented bedroom premium.
        records = [sale(i + 1, beds=beds, ppsf=ppsf, lat=37.8)
                   for i, (beds, ppsf) in enumerate(((2, 700), (2, 750),
                       (3, 1000), (3, 1050), (4, 600), (4, 650), (5, 800), (8, 850)))]
        results = {beds: estimate(records, 37.8, -122.25, beds=beds, as_of=AS_OF)
                   for beds in (2, 3, 4, 5, 10)}
        self.assertLess(results[4]["estimate_ppsf"], results[3]["estimate_ppsf"])
        self.assertNotEqual(results[2]["estimate_ppsf"], results[3]["estimate_ppsf"])
        self.assertEqual(results[5]["estimate_ppsf"], results[10]["estimate_ppsf"])
        three = {comp["id"]: comp for comp in results[3]["comparables"]}
        four = {comp["id"]: comp for comp in results[4]["comparables"]}
        for identity, comp in three.items():
            for component in ("recency", "distance", "size"):
                self.assertEqual(comp["weight_components"][component], four[identity]["weight_components"][component])
        two = {comp["id"]: comp for comp in results[2]["comparables"]}
        self.assertEqual(two["1"]["weight_components"]["bedrooms"], 1)
        self.assertEqual(three["1"]["weight_components"]["bedrooms"], .75)
        self.assertIn("causal", model_description(AS_OF)["limitations"])
        self.assertTrue(any("does not estimate" in note for note in results[3]["notes"]))

    def test_invalid_subject_is_rejected(self):
        for kwargs in ({"lat": float("nan")}, {"lng": 0}, {"sqft": 1999}, {"beds": 2.5}):
            values = {"lat": 37.8, "lng": -122.25}
            values.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                estimate([], **values, as_of=AS_OF)

    def test_geographic_distance(self):
        self.assertAlmostEqual(haversine_km(37.8, -122.25, 37.8, -122.25), 0)
        self.assertTrue(1.1 < haversine_km(37.8, -122.25, 37.81, -122.25) < 1.12)

    def test_distance_prefilter_preserves_radius_edges_and_skips_distant_rows(self):
        # Points around the whole circle, including its extreme east/west
        # longitudes, guard against a fast box that clips valid neighbors.
        from model import EARTH_RADIUS_KM
        for center_lat in (36.9, 37.8, 38.8):
            records = []
            for index, bearing in enumerate(range(0, 360, 15)):
                angle, direction = 2.99999 / EARTH_RADIUS_KM, math.radians(bearing)
                phi = math.radians(center_lat)
                lat = math.asin(math.sin(phi) * math.cos(angle) + math.cos(phi) * math.sin(angle) * math.cos(direction))
                lng = math.radians(-122.25) + math.atan2(math.sin(direction) * math.sin(angle) * math.cos(phi),
                                                       math.cos(angle) - math.sin(phi) * math.sin(lat))
                records.append(sale(index + 1, lat=math.degrees(lat), lng=math.degrees(lng)))
            records.extend(sale(100 + i, lat=37.3, lng=-121.5) for i in range(100))
            with patch("model.haversine_km", wraps=haversine_km) as distance:
                result = estimate(records, center_lat, -122.25, as_of=AS_OF)
                self.assertEqual(result["comparable_count"], 24)
                self.assertEqual(distance.call_count, 24)


if __name__ == "__main__":
    unittest.main()
