"""Offline tests of credit reservations, recovery, and atomic publication."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import apify_refresh as subject
from refresh import RefreshManager, atomic_json

TODAY = date(2026, 10, 29)
TOKEN = 'fake_apify_token_never_a_real_secret'


def paid_allowance(**changes):
    return dict({'enabled': True, 'id': 'initial-bay-area-import',
        'expires_at': (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        'total_budget_usd': 75, 'fixed_cash_paid_usd': 19,
        'included_usage_credit_usd': 19, 'tax_nonusage_reserve_usd': 5,
        'usage_budget_usd': 65, 'payment_verified': True, 'account_limit_verified': True}, **changes)


def sale(identifier='12345', **changes):
    row = {'id': identifier, 'address': identifier + ' Example Road, Oakland, CA 94611',
           'city': 'Oakland', 'lat': 37.83, 'lng': -122.23, 'price': 1500000,
           'sqft': 2500, 'beds': 3, 'sold_date': '2026-10-01', 'home_type': 'singleFamily',
           'source_url': 'https://www.zillow.com/homedetails/' + identifier + '_zpid/'}
    return dict(row, **changes)


def attached_evidence(identifier='12345', **changes):
    row = sale(identifier)
    return dict(row, source_id=identifier, reason='explicit_attached_home',
                attachment_evidence='Zillow resoFacts.hasAttachedProperty true', **changes)


class ProviderFailure(Exception):
    def __init__(self, code='network'):
        super().__init__(TOKEN)
        self.code = code


class FakeFactory:
    def __init__(self):
        self.starts, self.waits, self.datasets, self.options, self.inputs = [], [], [], [], []
        self.fail_at = None
        self.failure_code = 'network'
        self.items = [sale()]
        self.read_bytes = 512
        self.response_reserve_bytes = 4096
        self.crash_at = None
        self.request_check = None
        self.cap_reached = False
        self.reservation_check = None
        self.recoveries, self.gets, self.wait_failures = [], [], []
        self.remote_recovery_count = 0
        self.remote_timeout = 600
        self.accept_recovery = True
        self.get_changes = {}

    def __call__(self, token, **options):
        assert token == TOKEN
        self.options.append(options)
        owner = self

        class Client:
            read_bytes = 0

            def request(self, phase):
                options['before_request']('POST' if phase in {'start', 'resurrect'} else 'GET', '/safe/' + phase)
                options['reserve_response'](owner.response_reserve_bytes)
                if owner.request_check:
                    owner.request_check(phase)
                if phase == 'resurrect' and owner.accept_recovery:
                    owner.remote_recovery_count += 1
                    owner.remote_timeout = self.recovery_timeout
                if owner.crash_at == phase:
                    raise SystemExit('simulated process exit during request')
                self.read_bytes += owner.read_bytes
                if owner.fail_at == phase:
                    raise ProviderFailure(owner.failure_code)
                options['settle_response'](owner.response_reserve_bytes, owner.read_bytes)

            def start(self, payload):
                if owner.reservation_check:
                    owner.reservation_check()
                owner.starts.append('run' + str(len(owner.starts) + 1))
                owner.inputs.append(payload)
                owner.remote_recovery_count = 0
                owner.remote_timeout = options['wait_timeout']
                self.request('start')
                return {'id': owner.starts[-1]}

            def observed(self, run_id, status):
                return {'id': run_id, 'status': status, 'defaultDatasetId': 'dataset1',
                        'actId': 'actor1', 'max_total_charge_usd': options['max_total_charge_usd'],
                        'timeout_seconds': owner.remote_timeout, 'restart_on_error': False,
                        'resurrect_count': owner.remote_recovery_count, 'usageTotalUsd': .01}

            def get_run(self, run_id):
                owner.gets.append(run_id)
                self.request('get')
                return dict(self.observed(run_id, 'RUNNING' if owner.remote_recovery_count else 'TIMED-OUT'), **owner.get_changes)

            def resurrect_timeout(self, run, *, timeout_seconds):
                owner.recoveries.append((run['id'], timeout_seconds, options['max_total_charge_usd']))
                self.recovery_timeout = timeout_seconds
                self.request('resurrect')
                return self.observed(run['id'], 'RUNNING')

            def wait(self, run_id):
                owner.waits.append(run_id)
                self.request('wait')
                if owner.wait_failures:
                    raise ProviderFailure(owner.wait_failures.pop(0))
                return self.observed(run_id, 'SUCCEEDED')

            def dataset(self, run, **kwargs):
                owner.datasets.append(run['id'])
                self.request('dataset')
                return {'items': owner.items, 'item_count': len(owner.items),
                        'dataset_complete': True, 'cap_reached': owner.cap_reached}

        return Client()


class ApifyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = RefreshManager(self.root)
        self.factory = FakeFactory()
        self.diagnostics = {}
        self.cfg = {'api_key': TOKEN, 'apify_free_plan_verified': True,
                    'apify_credit_budget_usd': 1.0, 'apify_max_cost_per_run_usd': 0.08,
                    'apify_max_runs_per_refresh': 1, 'apify_max_items_per_county': 100000}

    def run_refresh(self):
        return subject.run_refresh(self.manager, self.cfg, client_factory=self.factory,
                                   normalize=lambda items, **kwargs: {'sales': items, 'diagnostics': self.diagnostics},
                                   input_builder=lambda locations, **kwargs: {'locations': locations, **kwargs},
                                   as_of=TODAY)

    def original(self, rows=None):
        data = {'sales': rows or [sale('99999')], 'source': 'Saved source', 'custom': 'preserve me'}
        self.manager.data.mkdir(parents=True, exist_ok=True)
        path = self.manager.data / 'sales.json'
        raw = json.dumps(data, indent=2).encode()
        path.write_bytes(raw)
        return path, raw

    def state(self):
        return json.loads((self.manager.data / 'apify-state.json').read_text())

    def test_free_plan_verification_required_before_any_request_or_reservation(self):
        self.cfg['apify_free_plan_verified'] = False
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertFalse(self.factory.starts)
        self.assertFalse((self.manager.data / 'apify-state.json').exists())

    def test_reserves_actor_cap_and_api_buffer_before_start_and_counts_http(self):
        self.factory.reservation_check = lambda: self.assertEqual(subject.status(self.root)['reserved_usd'], 0.10)
        result = self.run_refresh()
        self.assertTrue(result['published'])
        self.assertEqual(self.factory.options[0]['max_total_charge_usd'], 0.08)
        self.assertEqual(subject.status(self.root)['http_calls'], 3)
        self.assertEqual(self.state()['generation']['jobs'][0]['read_bytes'], 1536)
        self.assertEqual(stat.S_IMODE((self.manager.data / 'apify-state.json').stat().st_mode), 0o600)
        self.assertNotIn(TOKEN, json.dumps(subject.status(self.root)))
        self.assertNotIn(TOKEN, (self.manager.data / 'apify-state.json').read_text())

    def test_default_ten_cent_allowance_fits_eight_cent_run_plus_buffer(self):
        del self.cfg['apify_max_cost_per_run_usd']
        self.cfg['apify_credit_budget_usd'] = 0.10
        self.run_refresh()
        self.assertEqual(len(self.factory.starts), 1)
        self.assertEqual(subject.status(self.root)['credit_reserved_usd'], 0.10)

    def test_persisted_market_quarantine_blocks_cached_event_and_preserves_metadata(self):
        from test_market_quarantine import quarantine
        event = sale()
        metadata = quarantine(event)
        path, _ = self.original([event])
        path.write_text(json.dumps({'sales': [event], 'market_sale_quarantine': [metadata]}))
        self.factory.items = [event, sale('56789')]
        result = self.run_refresh()
        self.assertTrue(result['published'])
        saved = json.loads(path.read_text())
        self.assertEqual(saved['market_sale_quarantine'], [metadata])
        self.assertEqual([row['id'] for row in saved['sales']], ['56789'])
        counts = saved['last_api_refresh']['counts']
        self.assertEqual(counts['removed_quarantined'], 1)
        self.assertEqual(counts['excluded_quarantined'], 1)

    def enable_paid(self, **changes):
        self.cfg.update(apify_free_plan_verified=False, apify_api_buffer_usd=0.25,
                        apify_paid_allowance=paid_allowance(**changes))

    def test_response_reservation_is_durable_before_io_and_settles_afterward(self):
        observed = []
        self.factory.request_check = lambda phase: observed.append(
            (phase, self.state()['generation']['jobs'][0]['read_bytes']))
        self.run_refresh()
        self.assertEqual(observed, [('start', 4096), ('wait', 4608), ('dataset', 5120)])
        self.assertEqual(self.state()['generation']['jobs'][0]['read_bytes'], 1536)

    def test_process_crash_keeps_download_allowance_and_known_run_on_resume(self):
        self.cfg['apify_credit_budget_usd'] = 0.10
        self.factory.crash_at = 'dataset'
        with self.assertRaises(SystemExit):
            self.run_refresh()
        first = self.state()['generation']['jobs'][0]
        self.assertEqual(first['read_bytes'], 5120)
        self.assertEqual(first['http_calls'], 3)
        self.assertEqual(first['run_id'], 'run1')
        self.factory.crash_at = None
        self.manager = RefreshManager(self.root)
        self.assertTrue(self.run_refresh()['published'])
        self.assertEqual(len(self.factory.starts), 1)
        self.assertEqual(self.factory.options[1]['max_total_response_bytes'], subject.MAX_RESPONSE_BYTES - 5120)
        self.assertEqual(self.state()['generation']['jobs'][0]['read_bytes'], 6144)

    def test_uncertain_download_can_exhaust_budget_without_another_request(self):
        self.factory.crash_at = 'dataset'
        self.factory.response_reserve_bytes = subject.MAX_RESPONSE_BYTES - 1024
        with self.assertRaises(SystemExit):
            self.run_refresh()
        self.assertEqual(self.state()['generation']['jobs'][0]['read_bytes'], subject.MAX_RESPONSE_BYTES)
        requests = len(self.factory.options)
        self.factory.crash_at = None
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertEqual(len(self.factory.options), requests)
        self.assertEqual(len(self.factory.starts), 1)

    def test_paid_allowance_includes_subscription_credit_without_double_counting(self):
        self.enable_paid()
        self.assertTrue(self.run_refresh()['published'])
        state = self.state()
        self.assertEqual(state['paid_allowance']['usage_budget_micro_usd'], 65_000_000)
        self.assertEqual(state['reservations'][0]['paid_allowance_id'], 'initial-bay-area-import')
        self.assertEqual(subject.status(self.root)['paid_allowance']['reserved_usd'], 0.33)
        self.assertNotIn('initial-bay-area-import', json.dumps(subject.status(self.root)))

    def test_paid_allowance_is_pinned_only_when_paid_run_is_reserved(self):
        self.run_refresh()
        old_reservation = self.state()['reservations'][0]
        self.enable_paid(usage_budget_usd=0.1)
        self.assertTrue(self.run_refresh()['budget_reached'])
        self.assertNotIn('paid_allowance', self.state())
        self.cfg['apify_paid_allowance']['usage_budget_usd'] = 1
        self.run_refresh()
        self.assertEqual(self.state()['reservations'][0], old_reservation)
        self.assertEqual(subject.status(self.root)['reserved_usd'], 0.43)
        self.assertEqual(subject.status(self.root)['paid_allowance']['reserved_usd'], 0.33)

    def test_paid_grant_fails_closed_for_missing_verifications_or_invalid_budget(self):
        cases = [{'enabled': False}, {'payment_verified': False}, {'account_limit_verified': False},
                 {'total_budget_usd': 76}, {'usage_budget_usd': 75}, {'tax_nonusage_reserve_usd': 0},
                 {'fixed_cash_paid_usd': -1}, {'included_usage_credit_usd': 'Infinity'},
                 {'expires_at': '2030-01-01'}, {'id': 'untrusted space'},
                 {'expires_at': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.enable_paid(**changes)
                self.cfg['apify_free_plan_verified'] = True
                self.assertEqual(self.run_refresh()['state'], 'error')
                self.assertFalse(self.factory.starts)
                self.assertFalse((self.manager.data / 'apify-state.json').exists())

    def test_paid_allowance_cannot_be_raised_extended_replaced_or_removed(self):
        self.enable_paid()
        self.run_refresh()
        approved = dict(self.cfg['apify_paid_allowance'])
        for changes in ({'usage_budget_usd': 66}, {'id': 'second-import'}, {'enabled': False},
                        {'expires_at': (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()}):
            with self.subTest(changes=changes):
                self.cfg['apify_paid_allowance'] = dict(approved, **changes)
                self.assertEqual(self.run_refresh()['state'], 'error')
                self.assertEqual(len(self.factory.starts), 1)
        del self.cfg['apify_paid_allowance']
        self.cfg['apify_free_plan_verified'] = True
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertEqual(len(self.factory.starts), 1)

    def test_paid_allowance_cannot_start_another_generation_after_completion(self):
        self.enable_paid()
        self.cfg['apify_max_runs_per_refresh'] = 9
        self.assertEqual(self.run_refresh()['counties_completed'], 9)
        generation = self.state()['generation']['id']
        self.assertTrue(self.run_refresh()['allowance_consumed'])
        self.assertEqual(len(self.factory.starts), 9)
        self.assertEqual(self.state()['generation']['id'], generation)
        self.assertTrue(subject.status(self.root)['paid_allowance']['generation_finished'])

    def test_paid_criteria_change_does_not_purchase_duplicate_counties(self):
        self.enable_paid()
        self.run_refresh()
        self.cfg['apify_max_items_per_county'] = 50000
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertEqual(len(self.factory.starts), 1)

    def test_completed_paid_generation_recovers_publication_after_process_crash(self):
        self.enable_paid()
        self.cfg['apify_max_runs_per_refresh'] = 9
        with patch.object(subject, '_publish', side_effect=SystemExit('crashed before publish')):
            with self.assertRaises(SystemExit):
                self.run_refresh()
        self.assertEqual(len(self.factory.starts), 9)
        self.assertFalse((self.manager.data / 'sales.json').exists())
        self.assertTrue(subject.status(self.root)['paid_allowance']['publication_pending'])
        self.manager = RefreshManager(self.root)
        # Completed local publication is allowed after expiry; no billable call
        # is made, so an expiry guard must not block this recovery branch.
        with patch.object(subject, '_check_paid_expiry', side_effect=AssertionError('No network authorized')):
            result = self.run_refresh()
        self.assertTrue(result['published'])
        self.assertTrue(result['allowance_consumed'])
        self.assertEqual(len(self.factory.starts), 9)
        self.assertEqual(len(self.factory.options), 9)
        self.assertTrue((self.manager.data / 'sales.json').exists())
        self.assertFalse(subject.status(self.root)['paid_allowance']['publication_pending'])
        self.assertFalse(self.run_refresh()['published'])

    def test_empty_paid_publication_is_checked_once_without_permanent_pending_state(self):
        self.enable_paid()
        self.factory.items = []
        self.cfg['apify_max_runs_per_refresh'] = 9
        with patch.object(subject, '_publish', side_effect=SystemExit('crashed before empty publish')):
            with self.assertRaises(SystemExit):
                self.run_refresh()
        self.assertTrue(subject.status(self.root)['paid_allowance']['publication_pending'])
        result = self.run_refresh()
        self.assertFalse(result['published'])
        self.assertFalse(subject.status(self.root)['paid_allowance']['publication_pending'])
        self.assertEqual(len(self.state()['generation']['publication_checked_run_ids']), 9)

    def test_production_publication_reapplies_cutoff_after_utc_midnight(self):
        self.original()
        self.factory.items = [sale('12345', sold_date='2024-10-29'), sale('45678')]
        with patch.object(subject, 'today_utc', side_effect=[TODAY, date(2026, 10, 30)]):
            result = subject.run_refresh(self.manager, self.cfg, client_factory=self.factory,
                normalize=lambda items, **kwargs: {'sales': items},
                input_builder=lambda locations, **kwargs: {'locations': locations, **kwargs})
        self.assertTrue(result['published'])
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertEqual({row['id'] for row in snapshot['sales']}, {'99999', '45678'})
        self.assertEqual(self.state()['generation']['as_of'], '2026-10-29')

    def test_explicit_as_of_remains_deterministic_at_publication(self):
        self.factory.items = [sale('12345', sold_date='2024-10-29'), sale('45678')]
        with patch.object(subject, 'today_utc', return_value=date(2026, 10, 30)):
            self.assertTrue(self.run_refresh()['published'])
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertEqual({row['id'] for row in snapshot['sales']}, {'12345', '45678'})

    def test_expired_incomplete_generation_publishes_cached_successes_without_api(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        self.enable_paid(expires_at=expiry.isoformat())
        self.original()
        with patch.object(subject, '_publish', side_effect=SystemExit('crash before publication')):
            with self.assertRaises(SystemExit):
                self.run_refresh()
        saved = self.state()
        self.assertEqual(saved['generation']['jobs'][0]['state'], 'succeeded')
        self.assertEqual(saved['generation']['jobs'][1]['state'], 'pending')
        requests = len(self.factory.options)
        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return expiry + timedelta(seconds=1)
        with patch.object(subject, 'datetime', ExpiredClock):
            result = self.run_refresh()
            self.assertEqual(result['state'], 'partial')
            self.assertTrue(result['published'])
            self.assertTrue(result['allowance_expired'])
            self.assertEqual(result['counties_completed'], 1)
            before_repeat = (self.manager.data / 'sales.json').read_bytes()
            self.assertFalse(self.run_refresh()['published'])
            self.assertEqual((self.manager.data / 'sales.json').read_bytes(), before_repeat)
        current = self.state()
        self.assertEqual(current['reservations'], saved['reservations'])
        self.assertEqual(current['paid_allowance'], saved['paid_allowance'])
        self.assertEqual(current['generation']['jobs'], saved['generation']['jobs'])
        self.assertEqual(current['generation']['id'], saved['generation']['id'])
        self.assertEqual(len(self.factory.options), requests)
        self.assertEqual(self.factory.starts, ['run1'])
        self.assertIn('no API request', self.manager.current['message'])
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertEqual({row['id'] for row in snapshot['sales']}, {'99999', '12345'})
        self.assertFalse(snapshot['coverage']['complete'])
        self.assertEqual(snapshot['coverage']['counties_completed'], 1)

    def test_expiry_is_checked_before_every_http_request(self):
        self.enable_paid()
        original_check = subject._check_paid_expiry
        checks = 0
        def expire_during_run(record):
            nonlocal checks
            checks += 1
            if checks == 4:  # validation, reservation, POST, then first GET
                raise subject.ApifyRefreshError('Expired during run')
            return original_check(record)
        with patch.object(subject, '_check_paid_expiry', side_effect=expire_during_run):
            self.assertEqual(self.run_refresh()['state'], 'error')
        job = self.state()['generation']['jobs'][0]
        self.assertEqual(job['http_calls'], 1)
        self.assertEqual(job['run_id'], 'run1')
        self.assertEqual(subject.status(self.root)['paid_allowance']['reserved_usd'], 0.33)

    def test_county_caps_and_long_run_timeout_are_bounded_and_persisted(self):
        self.enable_paid()
        caps = {county: (index + 1) / 10 for index, county in enumerate(subject.BAY_COUNTIES)}
        self.cfg.update(apify_county_run_caps_usd=caps, apify_run_timeout_seconds=3600,
                        apify_poll_interval_seconds=30, apify_max_runs_per_refresh=2)
        self.run_refresh()
        self.assertEqual([value['max_total_charge_usd'] for value in self.factory.options], [0.1, 0.2])
        self.assertTrue(all(value['wait_timeout'] == 3600 and value['poll_interval'] == 30
                            for value in self.factory.options))
        self.assertEqual(subject.status(self.root)['paid_allowance']['reserved_usd'], 0.8)

    def test_invalid_county_cap_or_timeout_never_reserves_or_dispatches(self):
        for changes in ({'apify_county_run_caps_usd': {'Alameda County, CA': 1}},
                        {'apify_run_timeout_seconds': 21601}, {'apify_poll_interval_seconds': True},
                        {'apify_poll_interval_seconds': 301},
                        {'apify_max_cost_per_run_usd': 101}):
            with self.subTest(changes=changes):
                cfg = dict(self.cfg)
                self.cfg.update(changes)
                self.assertEqual(self.run_refresh()['state'], 'error')
                self.assertFalse(self.factory.starts)
                self.cfg = cfg

    def test_paid_timeout_resurrects_same_run_once_with_unchanged_reservation(self):
        self.enable_paid()
        self.cfg.update(apify_run_timeout_seconds=21600, apify_poll_interval_seconds=300)
        self.factory.wait_failures = ['run_timed_out']
        observed = []
        def check(phase):
            if phase == 'resurrect':
                saved = self.state()
                observed.append(saved['generation']['jobs'][0]['timeout_recovery']['phase'])
                self.assertEqual(len(saved['reservations']), 1)
                self.assertEqual(saved['reservations'][0]['micro_usd'], 330000)
        self.factory.request_check = check
        result = self.run_refresh()
        self.assertTrue(result['published'])
        job = self.state()['generation']['jobs'][0]
        self.assertEqual(observed, ['intent'])
        self.assertEqual(job['timeout_recovery']['phase'], 'confirmed')
        self.assertEqual(job['timeout_recovery']['prior_resurrect_count'], 0)
        self.assertEqual(self.factory.starts, ['run1'])
        self.assertEqual(self.factory.recoveries, [('run1', 21600, .08)])
        self.assertEqual(self.factory.waits, ['run1', 'run1'])
        self.assertEqual(job['http_calls'], 6)
        self.assertEqual(job['read_bytes'], 6 * 512)
        self.assertEqual(subject.status(self.root)['paid_allowance']['reserved_usd'], .33)

    def test_free_timeout_never_resurrects(self):
        self.factory.wait_failures = ['run_timed_out']
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertEqual(self.state()['generation']['jobs'][0]['state'], 'failed')
        self.assertFalse(self.factory.recoveries)

    def test_uncertain_recovery_is_reconciled_by_get_without_second_post(self):
        self.enable_paid(usage_budget_usd=.33)
        self.factory.wait_failures = ['run_timed_out']
        self.factory.fail_at = 'resurrect'
        self.assertEqual(self.run_refresh()['state'], 'error')
        saved = self.state()
        old_job = saved['generation']['jobs'][0]
        self.assertEqual(old_job['state'], 'needs_review')
        self.assertEqual(old_job['timeout_recovery']['phase'], 'intent')
        self.assertIn('recovery', subject.status(self.root)['paused_reason'])
        self.factory.fail_at = None
        self.manager = RefreshManager(self.root)
        self.assertTrue(self.run_refresh()['published'])
        job = self.state()['generation']['jobs'][0]
        self.assertEqual(len(self.factory.recoveries), 1)
        self.assertEqual(self.factory.starts, ['run1'])
        self.assertEqual(self.state()['reservations'], saved['reservations'])
        self.assertGreater(job['http_calls'], old_job['http_calls'])
        self.assertGreater(job['read_bytes'], old_job['read_bytes'])

    def test_uncertain_unaccepted_recovery_never_reposts(self):
        self.enable_paid()
        self.factory.wait_failures = ['run_timed_out']
        self.factory.fail_at = 'resurrect'
        self.factory.accept_recovery = False
        self.run_refresh()
        self.factory.fail_at = None
        for _ in range(2):
            self.manager = RefreshManager(self.root)
            self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertEqual(len(self.factory.recoveries), 1)
        self.assertEqual(self.factory.starts, ['run1'])
        self.assertFalse(self.factory.datasets)
        self.assertEqual(self.state()['generation']['jobs'][0]['timeout_recovery']['phase'], 'intent')

    def test_crash_after_recovery_intent_before_post_stays_get_only(self):
        self.enable_paid()
        self.factory.wait_failures = ['run_timed_out']
        original_save = subject._save
        def crash(root, state):
            original_save(root, state)
            if state['generation']['jobs'][0].get('timeout_recovery'):
                raise SystemExit('after durable intent, before network')
        with patch.object(subject, '_save', side_effect=crash), self.assertRaises(SystemExit):
            self.run_refresh()
        self.assertFalse(self.factory.recoveries)
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertFalse(self.factory.recoveries)
        self.assertEqual(self.factory.starts, ['run1'])

    def test_second_timeout_is_terminal_and_cannot_be_resurrected_again(self):
        self.enable_paid()
        self.factory.wait_failures = ['run_timed_out', 'run_timed_out']
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertEqual(self.state()['generation']['jobs'][0]['state'], 'failed')
        self.run_refresh()
        self.assertEqual(len(self.factory.recoveries), 1)
        self.assertEqual(self.factory.starts, ['run1', 'run2'])

    def test_timeout_at_cost_limit_is_terminal_without_recovery(self):
        self.enable_paid()
        self.factory.wait_failures = ['run_timed_out']
        self.factory.get_changes = {'usageTotalUsd': .079}
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertEqual(self.state()['generation']['jobs'][0]['state'], 'failed')
        self.factory.get_changes = {}
        self.run_refresh()
        self.assertEqual(self.factory.starts, ['run1', 'run2'])
        self.assertFalse(self.factory.recoveries)

    def test_resume_slows_polling_without_changing_remote_timeout_or_counters(self):
        self.enable_paid(usage_budget_usd=.33)
        self.cfg.update(apify_run_timeout_seconds=3600, apify_poll_interval_seconds=30)
        self.factory.fail_at = 'wait'
        self.run_refresh()
        saved = self.state()
        old = saved['generation']['jobs'][0]
        self.factory.fail_at = None
        self.cfg.update(apify_run_timeout_seconds=21600, apify_poll_interval_seconds=300)
        self.run_refresh()
        current = self.state()['generation']['jobs'][0]
        self.assertEqual(current['run_timeout_seconds'], 3600)
        self.assertEqual(current['poll_interval_seconds'], 300)
        self.assertEqual(self.factory.options[-1]['wait_timeout'], 21600)
        self.assertEqual(self.factory.options[-1]['poll_interval'], 300)
        self.assertEqual(self.state()['reservations'], saved['reservations'])
        self.assertGreater(current['http_calls'], old['http_calls'])
        self.assertGreater(current['read_bytes'], old['read_bytes'])
        self.assertEqual(self.factory.starts, ['run1'])

    def test_start_and_recovery_timeouts_are_clamped_to_original_grant(self):
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=180)).isoformat()
        self.enable_paid(expires_at=expiry)
        self.cfg.update(apify_run_timeout_seconds=21600, apify_poll_interval_seconds=300)
        self.factory.wait_failures = ['run_timed_out']
        self.run_refresh()
        self.assertTrue(0 < self.factory.options[0]['wait_timeout'] <= 150)
        self.assertTrue(0 < self.factory.recoveries[0][1] <= 150)
        self.assertEqual(self.state()['paid_allowance']['expires_at'], expiry)

    def test_too_little_grant_time_never_reserves_or_starts(self):
        self.enable_paid(expires_at=(datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat())
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertFalse(self.factory.starts)
        self.assertFalse(self.state()['reservations'])

    def test_recovery_identity_or_limits_cannot_be_changed_on_reconcile(self):
        for changes in ({'defaultDatasetId': 'otherdata'}, {'actId': 'otheractor'},
                        {'resurrect_count': 2}, {'timeout_seconds': 601},
                        {'restart_on_error': True}, {'max_total_charge_usd': .07}):
            with self.subTest(changes=changes):
                self.setUp()
                self.enable_paid()
                self.factory.wait_failures = ['run_timed_out']
                self.factory.fail_at = 'resurrect'
                self.run_refresh()
                self.factory.fail_at = None
                self.factory.get_changes = changes
                self.assertEqual(self.run_refresh()['state'], 'error')
                self.assertEqual(len(self.factory.recoveries), 1)
                self.assertFalse(self.factory.datasets)

    def test_unconfirmed_start_timeout_cannot_be_bypassed_by_resume(self):
        self.enable_paid(usage_budget_usd=.33)
        self.factory.fail_at = 'wait'
        self.run_refresh()
        state = self.state()
        state['generation']['jobs'][0].update(requires_run_confirmation=True, state='needs_review')
        atomic_json(self.manager.data / 'apify-state.json', state, True)
        self.factory.fail_at = None
        self.factory.get_changes = {'timeout_seconds': 0}
        self.assertEqual(self.run_refresh()['state'], 'error')
        self.assertFalse(self.factory.datasets)
        self.factory.get_changes = {}
        self.assertTrue(self.run_refresh()['published'])
        self.assertEqual(self.factory.starts, ['run1'])

    def test_budget_survives_restart_and_never_resets_by_month(self):
        self.cfg['apify_credit_budget_usd'] = 0.10
        self.run_refresh()
        state = self.state()
        state['reservations'][0]['at'] = '2000-01-01T00:00:00Z'
        atomic_json(self.manager.data / 'apify-state.json', state, True)
        self.manager = RefreshManager(self.root)
        result = self.run_refresh()
        self.assertTrue(result['budget_reached'])
        self.assertEqual(len(self.factory.starts), 1)
        self.assertEqual(subject.status(self.root)['reserved_usd'], 0.10)

    def test_unknown_start_outcome_never_starts_again(self):
        path, raw = self.original()
        self.factory.fail_at = 'start'
        self.run_refresh()
        self.assertEqual(self.state()['generation']['jobs'][0]['state'], 'needs_review')
        self.factory.fail_at = None
        self.manager = RefreshManager(self.root)
        self.run_refresh()
        self.assertEqual(len(self.factory.starts), 1)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(subject.status(self.root)['reserved_usd'], 0.10)
        self.assertIsNotNone(subject.status(self.root)['paused_reason'])

    def test_known_run_resumes_without_second_actor_start_or_reservation(self):
        path, raw = self.original()
        self.factory.fail_at = 'wait'
        self.run_refresh()
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.state()['generation']['jobs'][0]['run_id'], 'run1')
        self.factory.fail_at = None
        self.cfg['apify_credit_budget_usd'] = 0.10
        self.manager = RefreshManager(self.root)
        result = self.run_refresh()
        self.assertTrue(result['published'])
        self.assertEqual(len(self.factory.starts), 1)
        self.assertEqual(self.factory.waits, ['run1', 'run1'])
        self.assertEqual(subject.status(self.root)['reserved_usd'], 0.10)
        self.assertEqual(subject.status(self.root)['http_calls'], 4)
        self.assertEqual(self.factory.options[1]['max_total_response_bytes'], subject.MAX_RESPONSE_BYTES - 512 - 4096)

    def test_failure_after_finished_county_preserves_prior_file_bytes(self):
        path, raw = self.original()
        self.cfg['apify_max_runs_per_refresh'] = 2
        count = 0

        def check():
            nonlocal count
            count += 1
            if count == 2:
                self.factory.fail_at = 'wait'

        self.factory.reservation_check = check
        self.run_refresh()
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.manager.data / 'snapshots').exists())
        self.assertEqual(self.state()['generation']['jobs'][0]['state'], 'succeeded')
        self.assertEqual(self.state()['generation']['jobs'][1]['state'], 'running')

    def test_all_counties_are_queued_once_including_noncity_county_searches(self):
        self.cfg['apify_max_runs_per_refresh'] = 9
        result = self.run_refresh()
        self.assertEqual([x['locations'][0] for x in self.factory.inputs], list(subject.BAY_COUNTIES))
        self.assertEqual(len(self.factory.starts), 9)
        self.assertEqual(result['counties_completed'], 9)
        self.assertFalse(result['coverage_complete'])
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertTrue(snapshot['coverage']['county_searches_finished'])
        self.assertFalse(snapshot['coverage']['complete'])
        self.assertEqual(snapshot['coverage']['status'], 'partial')

    def test_result_limit_is_reported_and_never_called_complete(self):
        self.cfg['apify_max_items_per_county'] = 1
        self.run_refresh()
        self.assertTrue(self.state()['generation']['jobs'][0]['result_limit_reached'])
        self.assertFalse(json.loads((self.manager.data / 'sales.json').read_text())['coverage']['complete'])

    def test_atomic_merge_preserves_existing_custom_data_and_newer_sales(self):
        self.original([sale(sold_date='2026-10-20', price=1750000), sale('99999')])
        self.factory.items = [sale(), sale('45678')]
        self.run_refresh()
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertEqual(snapshot['custom'], 'preserve me')
        self.assertEqual(len(snapshot['sales']), 3)
        saved = next(row for row in snapshot['sales'] if row['id'] == '12345')
        self.assertEqual(saved['price'], 1750000)
        self.assertEqual(saved['sold_date'], '2026-10-20')
        self.assertEqual(len(list((self.manager.data / 'snapshots').glob('sales-*.json'))), 1)

    def test_empty_or_ineligible_sales_do_not_claim_live_verification(self):
        path, raw = self.original()
        self.factory.items = [sale(sold_date='2020-01-01')]
        result = self.run_refresh()
        self.assertFalse(result['published'])
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(self.manager.current.get('live_verified', False))

    def test_damaged_credit_ledger_fails_closed_without_reset(self):
        self.manager.data.mkdir(parents=True)
        path = self.manager.data / 'apify-state.json'
        for content in ['{broken', '{}', '{"version":1,"reservations":[],"generation":{}}']:
            with self.subTest(content=content):
                path.write_text(content)
                self.assertEqual(self.run_refresh()['state'], 'error')
                self.assertFalse(self.factory.starts)
                self.assertEqual(path.read_text(), content)

    def test_corrupt_prior_dataset_stops_before_credit_reservation(self):
        self.manager.data.mkdir(parents=True)
        (self.manager.data / 'sales.json').write_text('{broken')
        self.run_refresh()
        self.assertFalse(self.factory.starts)
        self.assertFalse((self.manager.data / 'apify-state.json').exists())

    def test_invalid_costs_fail_closed(self):
        for value in [True, -1, 'NaN', 'Infinity', 0, 'invalid']:
            with self.subTest(value=value):
                self.cfg['apify_credit_budget_usd'] = value
                self.run_refresh()
                self.assertFalse(self.factory.starts)

    def test_provider_error_never_exposes_token(self):
        self.factory.fail_at = 'dataset'
        self.run_refresh()
        self.assertNotIn(TOKEN, json.dumps(self.manager.current))
        self.assertEqual(subject.status(self.root)['reserved_usd'], 0.10)

    def test_cached_publication_does_not_update_timestamps_or_verify_rotated_key(self):
        self.cfg['apify_credit_budget_usd'] = 0.10
        self.run_refresh()
        path = self.manager.data / 'sales.json'
        before = path.read_bytes()
        self.manager.current['live_verified'] = False
        self.manager.current['last_success'] = None
        result = self.run_refresh()
        self.assertFalse(result['published'])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.manager.current['live_verified'])
        self.assertIsNone(self.manager.current['last_success'])
        self.assertEqual(len(self.factory.starts), 1)

    def test_actor_cost_cap_is_reported_even_below_item_limit(self):
        self.factory.cap_reached = True
        self.run_refresh()
        job = self.state()['generation']['jobs'][0]
        self.assertTrue(job['provider_cap_reached'])
        self.assertFalse(job['result_limit_reached'])
        self.assertFalse(job['dataset_complete'])
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertTrue(snapshot['coverage']['regions'][subject.BAY_COUNTIES[0]]['provider_cap_reached'])
        self.assertFalse(snapshot['coverage']['complete'])

    def test_corrupt_resume_cap_or_reservation_identity_blocks_network(self):
        self.factory.fail_at = 'wait'
        self.run_refresh()
        original = self.state()
        cases = [
            lambda s: s['generation']['jobs'][0].update(actor_micro_usd=9000000),
            lambda s: s['reservations'][0].update(micro_usd=1),
            lambda s: s['reservations'][0].update(county='Napa County, CA'),
            lambda s: s['reservations'][0].update(generation='other'),
        ]
        for damage in cases:
            with self.subTest(damage=damage):
                state = json.loads(json.dumps(original))
                damage(state)
                atomic_json(self.manager.data / 'apify-state.json', state, True)
                before = len(self.factory.options)
                self.run_refresh()
                self.assertEqual(len(self.factory.options), before)
                self.assertEqual(self.manager.current['state'], 'error')

    def test_new_generations_request_details_by_default(self):
        self.run_refresh()
        self.assertTrue(self.state()['generation']['include_details'])
        self.assertTrue(self.factory.inputs[0]['include_details'])

    def test_changed_detail_criteria_archives_old_generation_and_preserves_costs(self):
        self.cfg['apify_include_details'] = False
        self.run_refresh()
        original = self.state()
        self.cfg['apify_include_details'] = True
        self.run_refresh()
        state = self.state()
        self.assertNotEqual(state['generation']['id'], original['generation']['id'])
        self.assertEqual(state['reservations'][0], original['reservations'][0])
        self.assertEqual(subject.status(self.root)['reserved_usd'], 0.20)
        archive = self.manager.data / 'apify-generations' / (original['generation']['id'] + '.json')
        self.assertEqual(json.loads(archive.read_text()), original['generation'])
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertTrue((self.manager.data / 'apify-cache' / original['generation']['id'] / '0.json').exists())
        self.assertEqual([item['include_details'] for item in self.factory.inputs], [False, True])

    def test_changed_item_limit_starts_new_generation_only_when_safe(self):
        self.run_refresh()
        original = self.state()['generation']['id']
        self.cfg['apify_max_items_per_county'] = 250
        self.run_refresh()
        self.assertNotEqual(self.state()['generation']['id'], original)
        self.assertEqual(self.factory.inputs[-1]['max_results'], 250)

    def test_active_run_keeps_original_criteria_and_defers_new_pending_jobs(self):
        self.cfg['apify_include_details'] = False
        self.factory.fail_at = 'wait'
        self.run_refresh()
        original = self.state()['generation']['id']
        self.cfg.update(apify_include_details=True, apify_max_items_per_county=250)
        self.factory.fail_at = None
        resumed = self.run_refresh()
        self.assertTrue(resumed['criteria_pending'])
        self.assertEqual(self.state()['generation']['id'], original)
        self.assertFalse(self.state()['generation']['include_details'])
        self.assertEqual(len(self.factory.starts), 1)
        self.assertEqual(subject.status(self.root)['reserved_usd'], 0.10)
        self.run_refresh()
        self.assertNotEqual(self.state()['generation']['id'], original)
        self.assertTrue(self.factory.inputs[-1]['include_details'])
        self.assertEqual(self.factory.inputs[-1]['max_results'], 250)

    def test_unknown_start_cannot_be_bypassed_by_criteria_or_budget_changes(self):
        self.factory.fail_at = 'start'
        self.run_refresh()
        original = self.state()['generation']['id']
        self.cfg.update(apify_include_details=False, apify_max_items_per_county=250,
                        apify_credit_budget_usd=5, apify_max_cost_per_run_usd=0.50)
        self.factory.fail_at = None
        self.run_refresh()
        self.assertEqual(self.state()['generation']['id'], original)
        self.assertEqual(len(self.factory.starts), 1)
        self.assertEqual(subject.status(self.root)['reserved_usd'], 0.10)
        self.assertEqual(self.manager.current['state'], 'error')

    def test_old_checkpoint_without_detail_flag_means_false(self):
        self.cfg['apify_include_details'] = False
        self.run_refresh()
        state = self.state()
        original = state['generation']['id']
        del state['generation']['include_details']
        atomic_json(self.manager.data / 'apify-state.json', state, True)
        self.run_refresh()
        self.assertEqual(self.state()['generation']['id'], original)
        self.assertFalse(self.factory.inputs[-1]['include_details'])

    def test_nonboolean_detail_setting_fails_before_request(self):
        for value in ['true', 1, None]:
            with self.subTest(value=value):
                self.cfg['apify_include_details'] = value
                self.assertEqual(self.run_refresh()['state'], 'error')
                self.assertFalse(self.factory.starts)

    def test_attached_evidence_removes_only_exact_zillow_property_and_publishes_without_sales(self):
        redfin = sale('redfin:12345', source_url='https://www.redfin.com/CA/Oakland/12345/home/12345')
        self.original([sale(), sale('99999'), redfin])
        self.factory.items = []
        self.diagnostics = {'explicit_attached_exclusions': [attached_evidence()]}
        result = self.run_refresh()
        self.assertTrue(result['published'])
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertEqual({row['id'] for row in snapshot['sales']}, {'99999', 'redfin:12345'})
        self.assertEqual(snapshot['last_api_refresh']['counts']['removed_attached'], 1)
        self.assertEqual(snapshot['coverage']['explicit_attached_exclusions'], 1)
        self.assertEqual(snapshot['excluded_attached_properties'][0]['source_id'], '12345')

    def test_attached_exclusions_persist_and_prevent_future_basic_feed_readdition(self):
        self.original()
        self.factory.items = []
        self.diagnostics = {'explicit_attached_exclusions': [attached_evidence()]}
        self.run_refresh()
        self.diagnostics = {}
        self.factory.items = [sale(), sale('45678')]
        self.run_refresh()
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertNotIn('12345', {row['id'] for row in snapshot['sales']})
        self.assertIn('45678', {row['id'] for row in snapshot['sales']})
        self.assertEqual(snapshot['last_api_refresh']['counts']['excluded_incoming_attached'], 1)
        self.assertEqual(len(snapshot['excluded_attached_properties']), 1)

    def test_missing_or_mismatched_attached_evidence_never_removes_baseline(self):
        self.original([sale()])
        wrong_id = attached_evidence()
        wrong_id['source_url'] = 'https://www.zillow.com/homedetails/99999_zpid/'
        missing_flag = attached_evidence()
        missing_flag['reason'] = 'missing_detachment_flag'
        self.factory.items = [sale('45678')]
        self.diagnostics = {'explicit_attached_exclusions': [wrong_id, missing_flag, None]}
        self.run_refresh()
        snapshot = json.loads((self.manager.data / 'sales.json').read_text())
        self.assertIn('12345', {row['id'] for row in snapshot['sales']})
        self.assertFalse(snapshot['excluded_attached_properties'])

    def test_attached_exclusions_require_validated_source_and_fixed_attachment_evidence(self):
        invalid = [
            {'attachment_evidence': None}, {'attachment_evidence': 'has an attached garage'},
            {'attachment_evidence': 'Zillow resoFacts.hasAttachedProperty false'},
            {'address': None}, {'address': ' '},
            {'source_url': 'https://www.zillow.com/other/12345_zpid/'},
            {'source_url': 'https://www.zillow.com/homedetails/12345_zpid/?unverified=yes'},
            {'source_url': 'https://www.zillow.com/homedetails/12345_zpid/#unverified'},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                evidence = attached_evidence()
                evidence.update(changes)
                self.assertIsNone(subject._attached_evidence(evidence))
        for evidence in ['Zillow record.isAttached true', 'Zillow resoFacts.is_detached false',
                         'Zillow record property subtype explicitly indicates an attached home']:
            with self.subTest(evidence=evidence):
                row = attached_evidence()
                row['attachment_evidence'] = evidence
                self.assertEqual(subject._attached_evidence(row)['source_id'], '12345')


if __name__ == '__main__':
    unittest.main()
