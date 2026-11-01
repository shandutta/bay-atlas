"""Reviewed ineligible sale events stay excluded; no real property fixtures."""
from datetime import date
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

from model import exclude_market_quarantine, merge_sales
from server import DataStore
from test_model import sale

TODAY = date(2026, 10, 29)


def quarantine(row=None, **changes):
    row = sale() if row is None else row
    return dict(row, source_id=row['id'], approved=True,
                decision='quarantine_pending_market_sale_verification',
                reason='Synthetic reviewed non-market transfer', **changes)


class MarketQuarantineTests(unittest.TestCase):
    def test_reviewed_multiunit_sale_does_not_exclude_a_later_eligible_resale(self):
        event = sale()
        later = dict(event, sold_date='2026-10-28')
        review = dict(quarantine(event), decision='quarantine_ineligible_property_sale',
                      reason='Synthetic sale combines a detached house and multiple rental units')
        kept, removed = exclude_market_quarantine([event, later], [review])
        self.assertEqual(kept, [later])
        self.assertEqual(removed, 1)

    def test_exact_source_event_excluded_without_hiding_a_later_or_corrected_sale(self):
        event = sale()
        later = dict(event, sold_date='2026-10-28')
        corrected = dict(event, price=event['price'] + 1)
        other = sale(2, price=event['price'])
        kept, removed = exclude_market_quarantine([event, later, corrected, other], [quarantine(event)])
        self.assertEqual(kept, [later, corrected, other])
        self.assertEqual(removed, 1)
        for replacement in (later, corrected):
            merged, counts = merge_sales([event], [replacement], TODAY, market_sale_quarantine=[quarantine(event)])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]['sold_date'], replacement['sold_date'])
            self.assertEqual(merged[0]['price'], replacement['price'])
            self.assertEqual(counts['removed_quarantined'], 1)

    def test_cross_provider_alias_requires_same_city_address_and_close_location(self):
        event = sale(address='100 Example Street, Oakland, CA', street_address='100 Example Street')
        alias = dict(event, id='redfin:5678', source_id='5678', source_url='https://www.redfin.com/CA/Oakland/Example/home/5678',
                     address='100 Example', street_address='100 Example', lat=event['lat'] + 0.00001)
        distinct = [dict(alias, city='Berkeley'), dict(alias, lat=38.0),
                    dict(alias, address='100 Example Road', street_address='100 Example Road'),
                    dict(alias, address='102 Example Street', street_address='102 Example Street')]
        kept, removed = exclude_market_quarantine([alias, *distinct], [quarantine(event)])
        self.assertEqual(kept, distinct)
        self.assertEqual(removed, 1)
        without_location = quarantine(event)
        without_location.pop('lat')
        self.assertEqual(exclude_market_quarantine([alias], [without_location]), ([alias], 0))

    def test_malformed_unapproved_or_broad_entries_never_hide_a_sale(self):
        event, approved = sale(), quarantine()
        mutations = [{'approved': False}, {'approved': 'true'}, {'decision': 'exclude'},
                     {'decision': {}}, {'decision': []}, {'source_id': '*'},
                     {'source_id': '9999'}, {'source_url': 'https://www.zillow.com/'}, {'source_url': 123},
                     {'source_url': 'https://www.zillow.com/not-a-listing/1_zpid/'},
                     {'source_url': 'https://www.zillow.com/homedetails/1_zpid/?q=other'},
                     {'source_url': 'https://www.zillow.com/homedetails/1_zpid/#other'},
                     {'source_url': 'https://www.zillow.com/homedetails/../1_zpid/'},
                     {'source_url': 'https://www.zillow.com/homedetails/%2e%2e/1_zpid/'},
                     {'source_url': 'https://zillow.com.evil.test/1_zpid/'}, {'sold_date': '*'},
                     {'sold_date': '2026-10-19T00:00:00Z'}, {'price': None}, {'price': True},
                     {'price': float('nan')}, {'price': float('inf')}, {'price': 0}, {'city': ''}]
        for change in mutations:
            with self.subTest(change=change):
                self.assertEqual(exclude_market_quarantine([event], [dict(approved, **change)]), ([event], 0))
        for entries in (None, {}, 'anything', [None, 1, [], {}]):
            self.assertEqual(exclude_market_quarantine([event], entries), ([event], 0))
        # A valid exclusion's provider identity must not hide an unrelated URL.
        malformed_row = dict(event, source_url='https://www.zillow.com/homedetails/9999_zpid/')
        self.assertEqual(exclude_market_quarantine([malformed_row], [approved]), ([malformed_row], 0))

    def test_merging_filters_before_dedup_and_never_mutates_audit_metadata(self):
        event, valid = sale(age=5), sale(age=20)
        metadata = quarantine(event)
        before = json.dumps(metadata, sort_keys=True)
        merged, counts = merge_sales([], [event, valid], TODAY, market_sale_quarantine=[metadata])
        self.assertEqual(merged[0]['sold_date'], valid['sold_date'])
        self.assertEqual(counts['excluded_quarantined'], 1)
        self.assertEqual(json.dumps(metadata, sort_keys=True), before)

    def test_load_defense_filters_saved_event_without_serving_private_audit_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sales.json'
            event, good = sale(), sale(2)
            path.write_text(json.dumps({'sales': [event, good], 'market_sale_quarantine': [quarantine(event)]}))
            with patch('server.today_utc', return_value=TODAY):
                snapshot = DataStore(path).snapshot()
            self.assertEqual([row['id'] for row in snapshot['sales']], ['2'])
            self.assertEqual(snapshot['diagnostics']['excluded']['quarantined_market_sale'], 1)
            self.assertNotIn('market_sale_quarantine', snapshot)

    def test_offline_snapshot_readers_honor_the_same_event_quarantine(self):
        from scripts.audit_coverage import audit
        from scripts.coverage_diagnostics import summarize
        from scripts.backtest_confidence import prepare_history
        event, good = sale(), sale(2)
        snapshot = {'sales': [event, good], 'market_sale_quarantine': [quarantine(event)]}
        self.assertEqual(audit(snapshot, as_of=TODAY)['summary']['eligible_homes'], 1)
        self.assertEqual(summarize(snapshot, as_of=TODAY)['current_eligible_count'], 1)
        history = prepare_history(snapshot, date(2026, 1, 1), TODAY)
        self.assertEqual([row['id'] for row in history], ['2'])
        # Bare row lists remain supported for callers without an envelope.
        self.assertEqual(audit([event, good], as_of=TODAY)['summary']['eligible_homes'], 2)

    def test_import_audit_excludes_quarantine_while_retaining_saved_row_counts(self):
        from scripts.audit_import import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            event, good = sale(), sale(2)
            saved = json.dumps({'sales': [event, good], 'market_sale_quarantine': [quarantine(event)]})
            (root / 'data' / 'sales.json').write_text(saved)
            (root / 'data' / 'apify-state.json').write_text(json.dumps({'generation': {'id': 'a' * 32, 'jobs': []}}))
            output = io.StringIO()
            argv = ['audit_import.py', '--root', str(root), '--as-of', TODAY.isoformat(), '--allow-in-progress']
            with patch.object(sys, 'argv', argv), patch.object(sys, 'path', list(sys.path)), contextlib.redirect_stdout(output):
                self.assertEqual(main(), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report['saved_row_count'], 2)
            self.assertEqual(report['published_row_count'], 1)
            self.assertEqual(report['current_eligible_count'], 1)
            self.assertEqual(report['quarantined_market_events'], 1)
            self.assertEqual((root / 'data' / 'sales.json').read_text(), saved)
