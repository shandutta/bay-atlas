from datetime import date
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('backtest_confidence', Path(__file__).parents[1] / 'scripts/backtest_confidence.py')
backtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backtest)


class BacktestLeakageTests(unittest.TestCase):
    def sale(self, identity, sold_date='2026-07-01', **updates):
        row = dict(id=str(identity), address=f'{identity} Example Ave, Oakland, CA', city='Oakland',
                   lat=37.8, lng=-122.25, price=2000000, sqft=2500, beds=3,
                   home_type='singleFamily', sold_date=sold_date,
                   source_url=f'https://www.zillow.com/homedetails/{identity}_zpid/')
        row.update(updates)
        return row

    def history(self, rows):
        return backtest.prepare_history(rows, date(2026, 5, 1), date(2026, 10, 31))

    def test_same_day_future_and_entire_subject_property_excluded(self):
        subject = self.sale(99, '2026-08-01')
        rows = [self.sale(1), self.sale(2), self.sale(3), self.sale(99, '2025-01-01'),
                self.sale(4, '2026-08-01'), self.sale(5, '2026-08-02'), subject]
        index = backtest.HistoricalIndex(self.history(rows))
        comps = index.comparables_before(subject, date(2026, 8, 1))
        self.assertEqual({row['id'] for row in comps}, {'1', '2', '3'})
        self.assertTrue(all(row['age_days'] == 31 for row in comps))

    def test_later_resale_does_not_erase_earlier_training_sale(self):
        subject = self.sale(99, '2026-08-01')
        index = backtest.HistoricalIndex(self.history([self.sale(1, '2026-01-01'), self.sale(1, '2026-09-01')]))
        comps = index.comparables_before(subject, date(2026, 8, 1))
        self.assertEqual([row['sold_date'] for row in comps], ['2026-01-01'])

    def test_two_year_cutoff_applies_at_prediction_date_not_end_date(self):
        subject = self.sale(99, '2026-05-01')
        index = backtest.HistoricalIndex(self.history([self.sale(1, '2024-05-01'), self.sale(2, '2024-04-30')]))
        comps = index.comparables_before(subject, date(2026, 5, 1))
        self.assertEqual([row['id'] for row in comps], ['1'])

    def test_cross_source_subject_alias_is_not_a_comparable(self):
        subject = self.sale(99, '2026-08-01')
        alias = self.sale(123, '2026-07-01', address='99 Example Avenue, Oakland, CA',
                          source_url='https://www.redfin.com/CA/Oakland/99-Example-Ave/home/123')
        index = backtest.HistoricalIndex(self.history([alias]))
        self.assertEqual(index.comparables_before(subject, date(2026, 8, 1)), [])

    def test_missing_suffix_same_location_subject_alias_excluded(self):
        subject = self.sale(99, '2026-08-01')
        alias = self.sale(123, '2026-07-01', address='99 Example, Oakland, CA')
        index = backtest.HistoricalIndex(self.history([alias]))
        self.assertEqual(index.comparables_before(subject, date(2026, 8, 1)), [])

    def test_output_is_aggregate_only_and_no_future_support(self):
        rows = [self.sale(1, '2026-08-01'), self.sale(2, '2026-08-01'), self.sale(3, '2026-08-01')]
        result = backtest.backtest(rows, date(2026, 5, 1), date(2026, 8, 1), date(2026, 10, 31), bootstrap=0)
        validation = result['periods']['validation']['all']
        self.assertEqual(validation['held_out_homes'], 3)
        self.assertEqual(validation['available_predictions'], 0)
        self.assertNotIn('Example', str(result))
        self.assertNotIn('zillow', str(result))


if __name__ == '__main__':
    unittest.main()
