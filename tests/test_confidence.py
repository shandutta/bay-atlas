import unittest
from datetime import date
from confidence import assess_confidence


class ConfidenceTests(unittest.TestCase):
    def estimate(self, count=24, spread=.004, age='2026-10-01'):
        comps = []
        for i in range(count):
            # A distributed ring with a small price range, not an identical-location cluster.
            import math
            angle = 2 * math.pi * i / count
            comps.append({'lat': 37.8 + spread * math.sin(angle), 'lng': -122.3 + spread * math.cos(angle),
                          'weight': 1, 'price_per_sqft': 800 + (i % 3) * 10, 'sold_date': age,
                          'distance_km': .45})
        return {'as_of': '2026-10-31', 'subject': {'lat': 37.8, 'lng': -122.3},
                'estimate_ppsf': 810, 'comparables': comps}

    def test_distributed_recent_evidence_meets_target(self):
        r = assess_confidence(self.estimate())
        self.assertTrue(r['target_met'])
        self.assertEqual(r['evidence_tier'], 'target')
        self.assertFalse(r['is_calibrated_probability'])
        self.assertEqual(r['resolution']['support_diameter_80_m'], 900)
        self.assertEqual(r['resolution']['shading_grid_m'], 250)

    def test_repeated_coordinates_do_not_create_spatial_support(self):
        r = assess_confidence(self.estimate(spread=0))
        self.assertFalse(r['target_met'])
        self.assertEqual(r['metrics']['effective_spatial_blocks'], 1)
        self.assertIn('spatially_concentrated', r['reasons'])

    def test_effective_count_detects_dominant_sale(self):
        e = self.estimate()
        e['comparables'][0]['weight'] = 1000
        r = assess_confidence(e)
        self.assertEqual(r['evidence_tier'], 'insufficient')
        self.assertLess(r['metrics']['effective_homes'], 2)

    def test_uses_evaluation_date_not_stale_age_field(self):
        e = self.estimate(age='2026-05-01')
        for c in e['comparables']: c['age_days'] = 0
        r = assess_confidence(e, date(2027, 1, 1))
        self.assertEqual(r['metrics']['recent_183_days'], 0)
        self.assertIn('older_evidence', r['reasons'])

    def test_wide_support_has_coarse_resolution(self):
        e = self.estimate()
        for c in e['comparables']: c['distance_km'] = 2.4
        r = assess_confidence(e)
        self.assertFalse(r['target_met'])
        self.assertEqual(r['resolution']['support_diameter_80_m'], 4800)
        self.assertIn('evidence_too_distant', r['reasons'])

    def test_extreme_price_block_is_flagged(self):
        e = self.estimate()
        for c in e['comparables'][:7]:
            c.update(price_per_sqft=3000, lat=37.804, lng=-122.296)
        r = assess_confidence(e)
        self.assertFalse(r['target_met'])
        self.assertGreater(r['metrics']['max_block_log_shift'], .1)

    def test_invalid_evidence_cannot_meet_target(self):
        e = self.estimate()
        e['comparables'][0]['weight'] = float('nan')
        r = assess_confidence(e)
        self.assertFalse(r['target_met'])
        self.assertEqual(r['metrics']['ignored_invalid_comparables'], 1)
        self.assertIn('invalid_comparables_ignored', r['reasons'])

    def test_empty_evidence_has_no_resolution(self):
        e = self.estimate(count=0)
        r = assess_confidence(e)
        self.assertEqual(r['evidence_tier'], 'insufficient')
        self.assertIsNone(r['resolution']['support_diameter_80_m'])

    def test_missing_point_estimate_is_never_target(self):
        e = self.estimate(); e['estimate_ppsf'] = None
        self.assertEqual(assess_confidence(e)['evidence_tier'], 'insufficient')

    def test_expired_sales_do_not_contribute(self):
        e = self.estimate(age='2024-10-30')
        self.assertEqual(assess_confidence(e)['evidence_tier'], 'insufficient')
        e = self.estimate(age='2024-10-31')
        self.assertEqual(assess_confidence(e)['metrics']['homes'], 24)

    def test_future_sales_do_not_contribute(self):
        e = self.estimate(age='2026-12-01')
        r = assess_confidence(e)
        self.assertEqual(r['evidence_tier'], 'insufficient')
        self.assertEqual(r['metrics'], {})


if __name__ == '__main__':
    unittest.main()
