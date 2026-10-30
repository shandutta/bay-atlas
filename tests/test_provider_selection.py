"""Provider switching must never reuse a different service's credential."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from refresh import RefreshManager, atomic_json, public_apify_allowance
from model import today_utc


HOUSE_KEY = 'house_test_credential_1234567890'
APIFY_KEY = 'apify_test_credential_1234567890'
NEW_KEY = 'replacement_test_credential_1234567890'


def paid_grant(**changes):
    return dict({'enabled':True, 'id':'private_payment_authorization',
                 'expires_at':(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                 'total_budget_usd':75, 'fixed_cash_paid_usd':19,
                 'included_usage_credit_usd':19, 'tax_nonusage_reserve_usd':2,
                 'usage_budget_usd':73, 'payment_verified':True,
                 'account_limit_verified':True}, **changes)


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = RefreshManager(Path(self.temp.name))

    def test_switching_to_apify_without_its_key_does_not_reuse_rapidapi_key(self):
        self.manager.configure({'provider_kind':'housedata', 'api_key':HOUSE_KEY})
        result = self.manager.configure({'provider_kind':'apify', 'scope':'east_bay'})
        self.assertFalse(result['configured'])
        self.assertEqual(self.manager.config()['api_key'], '')
        self.assertEqual(result['scope'], 'bay_area')
        self.assertEqual(self.manager.start()['state'], 'unconfigured')
        saved = json.loads(self.manager.config_path.read_text())
        self.assertEqual(saved['api_key'], HOUSE_KEY, 'Preserve the other provider for a future switch')

    def test_round_trip_provider_switch_keeps_credentials_isolated_and_private(self):
        self.manager.configure({'provider_kind':'housedata', 'api_key':HOUSE_KEY})
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY})
        self.assertEqual(self.manager.config()['api_key'], APIFY_KEY)
        self.manager.configure({'provider_kind':'housedata'})
        self.assertEqual(self.manager.config()['api_key'], HOUSE_KEY)
        self.manager.configure({'provider_kind':'apify'})
        self.assertEqual(self.manager.config()['api_key'], APIFY_KEY)
        public = json.dumps([self.manager.status(), self.manager.provider_status()])
        self.assertNotIn(HOUSE_KEY, public)
        self.assertNotIn(APIFY_KEY, public)
        self.assertEqual(stat.S_IMODE(self.manager.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.manager.config_path.parent.stat().st_mode), 0o700)

    def test_replacing_same_provider_key_requires_new_live_verification(self):
        for kind, old in [('housedata', HOUSE_KEY), ('apify', APIFY_KEY)]:
            with self.subTest(provider=kind):
                self.manager.configure({'provider_kind':kind, 'api_key':old})
                self.manager.update(state='success', live_verified=True, provider_kind=kind)
                self.assertTrue(self.manager.provider_status()['live_verified'])
                self.manager.configure({'provider_kind':kind, 'api_key':NEW_KEY})
                self.assertFalse(self.manager.provider_status()['live_verified'])
                self.assertFalse(self.manager.status()['live_verified'])

    def test_rotating_apify_token_resets_free_plan_confirmation(self):
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY,
                                'apify_free_plan_verified':True})
        self.assertTrue(self.manager.provider_status()['apify_free_plan_verified'])
        self.manager.configure({'provider_kind':'apify', 'api_key':NEW_KEY})
        self.assertFalse(self.manager.provider_status()['apify_free_plan_verified'])

    def test_rotation_cannot_reconfirm_free_plan_in_the_same_request(self):
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY,
                                'apify_free_plan_verified':True})
        self.manager.configure({'api_key':NEW_KEY, 'apify_free_plan_verified':True})
        self.assertFalse(self.manager.provider_status()['apify_free_plan_verified'])

    def install_paid_grant(self, **changes):
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY})
        cfg = self.manager.config()
        cfg.pop('api_key', None)
        cfg['apify_paid_allowance'] = paid_grant(**changes)
        atomic_json(self.manager.config_path, cfg, private=True)

    def test_browser_cannot_create_or_change_private_paid_authorization(self):
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY})
        before = self.manager.config_path.read_bytes()
        for payload in [{'apify_paid_allowance':paid_grant()}, {'apify_api_buffer_usd':.25},
                        {'apify_county_run_caps_usd':{}}, {'apify_run_timeout_seconds':3600},
                        {'apify_poll_interval_seconds':60}, {'payment_verified':True}]:
            with self.subTest(payload=list(payload)):
                with self.assertRaises(ValueError):
                    self.manager.configure(payload)
                self.assertEqual(self.manager.config_path.read_bytes(), before)
        self.install_paid_grant()
        before = self.manager.config_path.read_bytes()
        for field, value in [('apify_credit_budget_usd',100), ('apify_free_plan_verified',True),
                             ('apify_max_cost_per_run_usd',100), ('apify_max_runs_per_refresh',9)]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.manager.configure({field:value})
                self.assertEqual(self.manager.config_path.read_bytes(), before)

    def test_rotation_disables_private_allowance_and_requires_live_verification(self):
        self.install_paid_grant()
        self.manager.update(state='success', live_verified=True, provider_kind='apify')
        self.manager.configure({'api_key':NEW_KEY})
        cfg = self.manager.config()
        self.assertFalse(cfg['apify_paid_allowance']['enabled'])
        self.assertFalse(cfg['apify_free_plan_verified'])
        public = self.manager.provider_status()
        self.assertFalse(public['live_verified'])
        self.assertEqual(public['allowance']['state'], 'disabled')
        with patch('apify_source.ApifyClient') as client:
            self.manager._run(cfg)
        client.assert_not_called()

    def test_public_allowance_discloses_bounds_without_payment_or_credential_details(self):
        self.install_paid_grant()
        public = self.manager.provider_status()
        allowance = public['allowance']
        self.assertEqual(allowance['mode'], 'paid')
        self.assertEqual(allowance['state'], 'active')
        self.assertEqual(allowance['total_usd'], 75)
        self.assertEqual(allowance['used_usd'], 21)
        self.assertEqual(allowance['remaining_usd'], 54)
        self.assertEqual(allowance['usage_remaining_usd'], 73)
        encoded = json.dumps(public)
        for secret in [APIFY_KEY, 'private_payment_authorization', 'fixed_cash_paid',
                       'payment_verified', 'included_usage_credit', 'account_limit_verified']:
            self.assertNotIn(secret, encoded)
        self.assertFalse(public['apify_free_plan_verified'], 'Paid mode does not claim the account is free')

    def test_switching_away_retains_safe_paid_mode_for_returning_to_apify(self):
        self.install_paid_grant()
        public = self.manager.configure({'provider_kind':'housedata', 'api_key':HOUSE_KEY})
        self.assertEqual(public['apify_allowance_mode'], 'paid')
        self.assertNotIn('private_payment_authorization', json.dumps(public))
        returned = self.manager.configure({'provider_kind':'apify'})
        self.assertEqual(returned['allowance']['state'], 'active')
        self.assertEqual(self.manager.config()['api_key'], APIFY_KEY)

    def test_public_allowance_marks_expired_exhausted_finished_and_damaged_ledgers(self):
        cfg = {'apify_paid_allowance':paid_grant(), 'apify_max_cost_per_run_usd':.08,
               'apify_api_buffer_usd':.25}
        budget = {'paid_allowance':{'reserved_usd':72.97}, 'jobs':[{'county':'Alameda County, CA','state':'pending'}]}
        self.assertEqual(public_apify_allowance(cfg, budget)['state'], 'exhausted')
        budget['jobs'] = [{'county':'Alameda County, CA','state':'running'}]
        self.assertEqual(public_apify_allowance(cfg, budget)['state'], 'active', 'Previously reserved work can resume')
        budget['paid_allowance']['generation_finished'] = True
        self.assertEqual(public_apify_allowance(cfg, budget)['state'], 'completed')
        cfg['apify_paid_allowance']['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.assertEqual(public_apify_allowance(cfg, budget)['state'], 'expired')
        damaged = public_apify_allowance(cfg, None)
        self.assertEqual(damaged['state'], 'invalid')
        self.assertIsNone(damaged['used_usd'])
        missing = public_apify_allowance({}, budget)
        self.assertEqual(missing['mode'], 'paid')
        self.assertEqual(missing['state'], 'invalid', 'A missing grant cannot silently become free mode')

    def test_expired_grant_can_offer_offline_publication_only_when_it_matches_saved_authorization(self):
        from apify_refresh import _paid_allowance
        cfg = {'apify_paid_allowance':paid_grant(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())}
        budget = {'paid_allowance':{'reserved_usd':3, 'generation_finished':True, 'publication_pending':True}}
        grant = _paid_allowance(cfg, check_expiry=False)
        saved = {'paid_allowance':dict(grant, generation_id='a' * 32)}
        with patch('apify_refresh._load', return_value=saved):
            self.assertEqual(public_apify_allowance(cfg, budget, self.manager.root)['state'], 'publication_pending')
            cfg['apify_paid_allowance']['enabled'] = False
            self.assertEqual(public_apify_allowance(cfg, budget, self.manager.root)['state'], 'disabled')
            cfg['apify_paid_allowance']['enabled'] = True
            cfg['apify_paid_allowance']['id'] = 'different_grant'
            self.assertEqual(public_apify_allowance(cfg, budget, self.manager.root)['state'], 'invalid')

    def test_unconfirmed_free_plan_cannot_start_external_calls(self):
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY})
        with patch('apify_source.ApifyClient') as client:
            self.manager._run(self.manager.config())
        client.assert_not_called()
        self.assertEqual(self.manager.status()['state'], 'error')

    def test_cached_sales_do_not_verify_a_replacement_token_when_budget_is_exhausted(self):
        from apify_refresh import run_refresh
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY,
                                'apify_free_plan_verified':True, 'apify_credit_budget_usd':0.10})
        row = {'id':'12345', 'address':'123 Test Road, Oakland, CA', 'city':'Oakland',
               'lat':37.82, 'lng':-122.25, 'price':2000000, 'sqft':2500, 'beds':3,
               'sold_date':today_utc().isoformat(), 'home_type':'singleFamily',
               'source_url':'https://www.zillow.com/homedetails/12345_zpid/'}
        clients = []

        class Client:
            read_bytes = 0
            def __init__(self, token, **kwargs):
                clients.append(token)
            def start(self, inputs):
                return {'id':'testRun12345'}
            def wait(self, run_id):
                return {'id':run_id, 'status':'SUCCEEDED', 'exitCode':0}
            def dataset(self, run, **kwargs):
                return {'items':[row], 'dataset_complete':True, 'cap_reached':False}

        arguments = {'client_factory':Client, 'normalize':lambda items, **kwargs:{'sales':items},
                     'input_builder':lambda locations, **kwargs:{'locations':locations}}
        run_refresh(self.manager, self.manager.config(), **arguments)
        self.assertTrue(self.manager.provider_status()['live_verified'])
        self.assertEqual(clients, [APIFY_KEY])
        saved = (self.manager.data / 'sales.json').read_bytes()
        self.manager.configure({'provider_kind':'apify', 'api_key':NEW_KEY,
                                'apify_free_plan_verified':True})
        run_refresh(self.manager, self.manager.config(), **arguments)
        self.assertEqual(clients, [APIFY_KEY], 'Exhausted credit allowance permits no new requests')
        self.assertFalse(self.manager.provider_status()['live_verified'])
        self.assertEqual((self.manager.data / 'sales.json').read_bytes(), saved)

    def test_invalid_budget_values_do_not_modify_saved_configuration(self):
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY})
        original = self.manager.config_path.read_bytes()
        for name in ['apify_max_cost_per_run_usd', 'apify_credit_budget_usd']:
            for value in [True, None, 0, -1, 0.001, 101, float('nan'), float('inf')]:
                with self.subTest(field=name, value=value):
                    with self.assertRaises(ValueError):
                        self.manager.configure({name:value})
                    self.assertEqual(self.manager.config_path.read_bytes(), original)
        for name in ['apify_max_runs_per_refresh', 'apify_max_items_per_county']:
            for value in [True, 0, 1.5, 100001]:
                with self.subTest(field=name, value=value):
                    with self.assertRaises(ValueError):
                        self.manager.configure({name:value})
                    self.assertEqual(self.manager.config_path.read_bytes(), original)

    def test_running_refresh_blocks_provider_and_credential_changes(self):
        self.manager.configure({'provider_kind':'housedata', 'api_key':HOUSE_KEY})
        original = self.manager.config_path.read_bytes()
        self.manager.update(state='running', provider_kind='housedata')
        with self.assertRaises(ValueError):
            self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY})
        self.assertEqual(self.manager.config_path.read_bytes(), original)

    def test_apify_details_default_true_and_persist_explicit_false(self):
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY})
        self.assertTrue(self.manager.config()['apify_include_details'])
        self.assertTrue(self.manager.provider_status()['apify_include_details'])
        self.manager.configure({'apify_include_details':False})
        self.assertFalse(self.manager.config()['apify_include_details'])
        self.assertFalse(self.manager.provider_status()['apify_include_details'])
        self.assertFalse(json.loads(self.manager.config_path.read_text())['apify_include_details'])

    def test_invalid_detail_flag_does_not_modify_configuration(self):
        self.manager.configure({'provider_kind':'apify', 'api_key':APIFY_KEY})
        original = self.manager.config_path.read_bytes()
        for value in ['true', 1, 0, None]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.manager.configure({'apify_include_details':value})
                self.assertEqual(self.manager.config_path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
