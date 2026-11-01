"""Synthetic schema and completed-sale evidence tests; no provider calls."""
from datetime import date
import unittest
import rapid_gap_source as source
from apify_refresh import _attached_evidence

TODAY = date(2026, 10, 31)


def sale():
    return {'zpid': '900001', 'url': 'https://www.zillow.com/homedetails/900001_zpid/',
            'homeStatus': 'RECENTLY_SOLD', 'detailsLevel': 'full', 'homeType': 'SINGLE_FAMILY',
            'street': '10 Example Rd', 'city': 'Oakland', 'state': 'CA', 'zip': '94611',
            'latitude': 37.83, 'longitude': -122.23, 'livingArea': 2500, 'bedrooms': 3,
            'soldDate': '2026-10-01', 'price': 1600000, 'zestimate': 1900000,
            'lastSoldDate': '2026-10-01', 'lastSoldPrice': 1501000,
            'priceHistory': [{'date': '2026-10-01', 'event': 'Sold', 'price': 1501000},
                             {'date': '2026-09-01', 'event': 'Listed for sale', 'price': 1600000}],
            'facts': {'hasAttachedProperty': False, 'hasAttachedGarage': True,
                      'propertySubType': ['Residential', 'Single Family Residence']}}


class RapidGapSourceTests(unittest.TestCase):
    def test_exact_completed_price_wins_and_private_extra_fields_dropped(self):
        item = sale(); item.update(agentPhone='private', photos=['private'], description='private')
        row = source.normalize_property(item, as_of=TODAY)
        self.assertEqual(row['price'], 1501000)
        self.assertEqual(row['sold_date'], '2026-10-01')
        self.assertIn('exact Sold event', row['price_evidence'])
        self.assertNotIn('private', str(row))

    def test_no_fallback_to_generic_price_or_paired_price_without_history(self):
        for history in [None, [], [{'date': '2026-10-01', 'event': 'Pending sale', 'price': 1501000}],
                        [{'date': '2025-10-01', 'event': 'Sold', 'price': 1200000}]]:
            item = sale(); item['priceHistory'] = history
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_conflicting_or_newer_dated_sale_evidence_rejected(self):
        for change in [{'lastSoldPrice': 999999}, {'lastSoldDate': '2026-10-02'},
                       {'priceHistory': [{'date': '2026-10-02', 'event': 'Sold', 'price': 1501000}]},
                       {'priceHistory': [{'date': '2026-10-01', 'event': 'Sold', 'price': 1501000},
                                         {'date': '2026-10-01', 'event': 'Sold', 'price': 1500000}]}]:
            item = sale(); item.update(change)
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_rental_events_cannot_supply_sale_price(self):
        item = sale(); item['priceHistory'][0]['postingIsRental'] = True
        self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_explicit_detachment_required_but_attached_garage_allowed(self):
        self.assertIsNotNone(source.normalize_property(sale(), as_of=TODAY))
        for value in [None, True, 'false']:
            item = sale(); item['facts']['hasAttachedProperty'] = value
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_attached_exclusion_requires_complete_eligible_sale(self):
        item = sale(); item['facts']['hasAttachedProperty'] = True
        result = source.normalize_records([item], as_of=TODAY)
        self.assertEqual(result['sales'], [])
        self.assertEqual(len(result['diagnostics']['explicit_attached_exclusions']), 1)
        item['priceHistory'] = []
        self.assertEqual(source.normalize_records([item], as_of=TODAY)['diagnostics']['explicit_attached_exclusions'], [])

    def test_attachment_provenance_round_trips_shared_exclusion_validator(self):
        item = sale(); item['facts']['hasAttachedProperty'] = True
        result = source.normalize_records([item], as_of=TODAY)
        raw = result['diagnostics']['explicit_attached_exclusions'][0]
        verified = _attached_evidence(raw)
        self.assertIsNotNone(verified)
        self.assertEqual(verified['attachment_evidence'], 'Zillow facts.hasAttachedProperty true')
        forged = dict(raw, attachment_evidence='Zillow invented.hasAttachedProperty true')
        self.assertIsNone(_attached_evidence(forged))

    def test_subtype_and_unit_override_single_family_flag(self):
        for change in [{'facts': {'hasAttachedProperty': False, 'propertySubType': ['Townhouse']}},
                       {'street': '10 Example Rd Unit 2'}]:
            item = sale(); item.update(change)
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_strict_identity_and_source_urls(self):
        for url in ['https://example.com/homedetails/900001_zpid/',
                    'https://www.zillow.com/homedetails/900002_zpid/',
                    'https://www.zillow.com/homedetails/900001_zpid/?token=secret',
                    'http://www.zillow.com/homedetails/900001_zpid/',
                    'https://user:secret@www.zillow.com/homedetails/900001_zpid/']:
            item = sale(); item['url'] = url
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_housing_criteria_geography_status_and_date_checked(self):
        for change in [{'livingArea': 1999}, {'livingArea': 3001}, {'bedrooms': 1},
                       {'homeType': 'TOWNHOUSE'}, {'homeStatus': 'FOR_SALE'}, {'detailsLevel': 'basic'},
                       {'state': 'NY'}, {'latitude': 40.8}, {'longitude': float('nan')},
                       {'soldDate': '2024-10-30'}, {'soldDate': '2026-11-01'}, {'soldDate': 42},
                       {'currency': 'CAD'}]:
            item = sale(); item.update(change)
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_actual_calendar_date_window_with_consistent_sale_evidence(self):
        for sold, expected in [('2024-10-30', False), ('2024-10-31', True), ('2026-11-01', False)]:
            item = sale()
            item['soldDate'] = item['lastSoldDate'] = item['priceHistory'][0]['date'] = sold
            result = source.normalize_records([item], as_of=TODAY)
            self.assertEqual(bool(result['sales']), expected)
            if not expected:
                self.assertEqual(result['diagnostics']['excluded'], {'outside_date_window': 1})

    def test_invalid_or_boolean_price_not_accepted(self):
        for price in [True, None, -1, float('inf')]:
            item = sale(); item['priceHistory'][0]['price'] = price
            self.assertIsNone(source.normalize_property(item, as_of=TODAY))

    def test_duplicates_are_one_home(self):
        result = source.normalize_records([sale(), sale()], as_of=TODAY)
        self.assertEqual(result['diagnostics']['eligible_count'], 1)
        self.assertEqual(result['diagnostics']['excluded']['duplicate_home'], 1)


if __name__ == '__main__':
    unittest.main()
