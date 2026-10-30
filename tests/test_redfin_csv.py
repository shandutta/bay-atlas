"""Test explicit closed-sale evidence and failure behavior; no network."""
import csv
from datetime import date
import hashlib
import io
import unittest

import redfin_csv as redfin

AS_OF = date(2026, 10, 29)
IMPORTED = '2026-10-29T13:40:00+00:00'


def home(**updates):
    row = {'SALE TYPE': 'PAST SALE', 'SOLD DATE': 'October-28-2026',
           'PROPERTY TYPE': 'Single Family Residential', 'ADDRESS': '100 Example St',
           'CITY': 'Berkeley', 'STATE OR PROVINCE': 'CA', 'ZIP OR POSTAL CODE': '94707',
           'PRICE': '2000000', 'BEDS': '3', 'SQUARE FEET': '2500', 'STATUS': 'Sold',
           'LATITUDE': '37.89', 'LONGITUDE': '-122.27',
           redfin.URL_HEADER: 'https://www.redfin.com/CA/Berkeley/100-Example-St-94707/home/1234',
           '$/SQUARE FEET': '799', 'SOURCE': 'Local MLS', 'MLS#': '1234567'}
    row.update(updates)
    return row


def csv_bytes(rows, headers=None):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=headers or list(home()))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8')


def parse(rows):
    return redfin.parse_csv(csv_bytes(rows), as_of=AS_OF, imported_at=IMPORTED)


class RedfinCSVTests(unittest.TestCase):
    def test_explicit_sale_price_date_and_provenance(self):
        payload = csv_bytes([home()])
        result = redfin.parse_csv(payload, as_of=AS_OF, imported_at=IMPORTED,
                                  export_filename='/private/export.csv')
        row = result['sales'][0]
        self.assertEqual(row['price'] / row['sqft'], 800)
        self.assertEqual(row['sold_date'], '2026-10-28')
        self.assertEqual(row['id'], 'redfin:1234')
        self.assertEqual(row['address'], '100 Example St, Berkeley, CA 94707')
        self.assertEqual(row['export_sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(row['export_filename'], 'export.csv')
        self.assertEqual(row['imported_at'], IMPORTED)
        self.assertNotIn('is_detached', row)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['coverage']['refresh_mode'], 'manual_csv')

    def test_notice_and_missing_date_never_become_sales(self):
        notice = {'SALE TYPE': 'In accordance with local MLS rules, some MLS listings are not included in the download'}
        result = parse([notice, home(**{'SOLD DATE': '', 'STATUS': ''})])
        self.assertEqual(result['sales'], [])
        self.assertEqual(result['diagnostics']['excluded'], {'mls_notice': 1, 'missing_sold_date': 1})

    def test_closed_status_and_valid_calendar_date_mandatory(self):
        for updates in [{'STATUS': ''}, {'STATUS': 'Active'}, {'SALE TYPE': 'FOR SALE'},
                        {'SOLD DATE': 'February-30-2026'}, {'SOLD DATE': '10/28/2026'},
                        {'SOLD DATE': 'October-30-2026'}, {'SOLD DATE': 'October-28-2024'}]:
            with self.subTest(updates=updates):
                self.assertEqual(parse([home(**updates)])['sales'], [])
        self.assertEqual(len(parse([home(**{'SOLD DATE': 'October-29-2024'})])['sales']), 1)

    def test_criteria_and_nonfinite_fields(self):
        for updates in [{'PROPERTY TYPE': 'Townhouse'}, {'PROPERTY TYPE': 'Single Family Attached'},
                        {'SQUARE FEET': '1999'}, {'SQUARE FEET': '3001'}, {'SQUARE FEET': ''},
                        {'BEDS': '1'}, {'BEDS': '2.5'}, {'PRICE': 'NaN'}, {'PRICE': '0'},
                        {'LATITUDE': 'Infinity'}, {'LATITUDE': '40'}, {'ADDRESS': ''}]:
            with self.subTest(updates=updates):
                self.assertEqual(parse([home(**updates)])['sales'], [])
        for sqft in ['2000', '3000']:
            self.assertEqual(len(parse([home(**{'SQUARE FEET': sqft, 'BEDS': '2'})])['sales']), 1)

    def test_source_url_cannot_impersonate_redfin_or_leak_credentials(self):
        for url in ['https://redfin.com.attacker.test/CA/Berkeley/100/home/1234',
                    'http://www.redfin.com/CA/Berkeley/100/home/1234',
                    'https://user:pass@www.redfin.com/CA/Berkeley/100/home/1234',
                    'https://www.redfin.com:444/CA/Berkeley/100/home/1234',
                    'https://www.redfin.com/']:
            with self.subTest(url=url):
                self.assertEqual(parse([home(**{redfin.URL_HEADER: url})])['sales'], [])

    def test_duplicate_properties_reported_without_destructive_merge(self):
        result = parse([home(), home(**{'SOLD DATE': 'August-1-2026'})])
        self.assertEqual(len(result['sales']), 2)
        self.assertEqual(result['diagnostics']['duplicate_source_ids'], 1)

    def test_schema_size_and_encoding_errors_fail_whole_import(self):
        for payload in [b'ADDRESS,PRICE\n100 Example,1000000\n', b'\xff', b'a' * (redfin.MAX_BYTES + 1)]:
            with self.subTest(size=len(payload)), self.assertRaises(redfin.RedfinCSVError):
                redfin.parse_csv(payload, as_of=AS_OF, imported_at=IMPORTED)
        with self.assertRaises(redfin.RedfinCSVError):
            redfin.parse_csv(csv_bytes([home()]), imported_at='2026-10-29T00:00:00')


if __name__ == '__main__':
    unittest.main()
