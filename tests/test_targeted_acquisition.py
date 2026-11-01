"""Offline one-time budget, narrow search, interruption and recovery checks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import targeted_acquisition as subject
from apify_source import ApifyError
from tests.test_apify_source import Queue, Response, sale

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
TOKEN = "fake_targeted_api_token_not_a_credential"


def manifest():
    return {"version": 1, "campaign_id": "synthetic-campaign", "as_of": "2026-10-31",
            "overhead_reserve_usd": "1.00", "targets": [
                {"id": "target-a", "label": "Synthetic target A", "search": {"bounds": {
                    "north": 37.9, "south": 37.8, "east": -122.2, "west": -122.3}},
                 "days": "12m", "max_results": 100, "actor_cap_usd": "0.245"},
                {"id": "target-b", "label": "Synthetic target B", "search": {"location": "Example, CA"},
                 "days": "24m", "max_results": 100, "actor_cap_usd": "0.245"}]}


def grant(plan):
    return {"version": 1, "campaign_id": plan["campaign_id"], "manifest_sha256": subject._hash(plan),
            "campaign_directory": "/private/synthetic-campaign",
            "enabled": True, "expires_at": (NOW + timedelta(hours=12)).isoformat(),
            "cash_limit_usd": "10", "fixed_cash_paid_usd": "0", "available_usage_credit_usd": "5",
            "tax_and_nonusage_reserve_usd": "1", "merchant_usage_limit_verified": True,
            "funding_verified": True, "account_verified_at": NOW.isoformat(), "funding_kind": "free_credit"}


class Factory:
    def __init__(self, directory):
        self.directory = directory
        self.starts = 0
        self.requests = []
        self.crash_start = False
        self.crash_dataset = False
        self.run_id_error = False
        self.status = "SUCCEEDED"
        self.options = []

    def __call__(self, token, **options):
        self.options.append(options)
        owner = self
        class Client:
            def request(self, method, path):
                options["before_request"](method, path)
                options["reserve_response"](1024)
                owner.requests.append((method, path))
                options["settle_response"](1024, 100)

            def verify_pricing(self):
                self.request("GET", "/v2/acts/topapi~zillow-scraper")

            def run(self):
                return {"id": "Run12345", "status": owner.status,
                        "max_total_charge_usd": options["max_total_charge_usd"],
                        "timeout_seconds": options["wait_timeout"], "restart_on_error": False,
                        "usageTotalUsd": .00245, "defaultDatasetId": "Data12345", "exitCode": 0}

            def start_target(self, target):
                ledger = json.loads((owner.directory / "ledger.json").read_text())
                assert ledger["jobs"][target["id"]]["state"] == "start_intent"
                assert ledger["usage_reserved_usd"] == 1.49
                self.request("POST", "/v2/acts/topapi~zillow-scraper/runs")
                owner.starts += 1
                if owner.run_id_error:
                    raise ApifyError("unconfirmed", "Unconfirmed run", run_id="Run12345")
                if owner.crash_start:
                    raise TimeoutError()
                return self.run()

            def get_run(self, run_id):
                self.request("GET", "/v2/actor-runs/" + run_id)
                return self.run()

            def wait(self, run_id):
                return self.get_run(run_id)

            def dataset(self, run, **kwargs):
                if owner.crash_dataset:
                    options["before_request"]("GET", "/v2/datasets/Data12345/items")
                    options["reserve_response"](2048)
                    raise TimeoutError()
                self.request("GET", "/v2/datasets/Data12345/items")
                return {"items": [sale()], "item_count": 1, "cap_reached": False}
        return Client()


class PlanTests(unittest.TestCase):
    def test_bounds_and_filters_are_explicit_and_details_required(self):
        value = subject.build_target_input(manifest()["targets"][0])
        self.assertNotIn("searchLocation", value)
        self.assertEqual(value["daysOnZillow"], "12m")
        self.assertEqual(value["homeTypes"], ["house"])
        self.assertEqual((value["minSqft"], value["maxSqft"], value["minBeds"]), (2000, 3000, 2))
        self.assertTrue(value["includeDetails"])
        self.assertFalse(value["monitorMode"])

    def test_rejects_loose_filters_unbounded_area_and_insufficient_cap(self):
        for change in ({"days": "36m"}, {"max_results": 0}, {"max_results": True},
                       {"actor_cap_usd": ".244999"}, {"unexpected": True},
                       {"search": {"bounds": {"north": 90, "south": 0, "east": 180, "west": 0}}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                subject.build_target_input({**manifest()["targets"][0], **change})

    def test_campaign_maximum_is_ten_dollars_with_overhead(self):
        plan = manifest()
        plan["targets"] = [{**plan["targets"][0], "id": str(i), "max_results": 800,
                            "actor_cap_usd": "1.96"} for i in range(4)]
        summary = subject.validate_manifest(plan)
        self.assertEqual(summary["maximum_detailed_rows"], 3200)
        self.assertEqual(summary["usage_ceiling_usd"], 8.84)
        paid = {**grant(plan), "available_usage_credit_usd": "0", "funding_kind": "paid_account"}
        subject.validate_grant(paid, plan, current_time=NOW)
        plan["targets"].append({**plan["targets"][0], "id": "fifth"})
        with self.assertRaises(subject.AcquisitionError):
            subject.validate_manifest(plan)

    def test_free_credit_and_cash_are_distinct_and_starter_is_rejected(self):
        plan = manifest()
        for changes in ({"available_usage_credit_usd": "0"}, {"fixed_cash_paid_usd": "19"},
                        {"cash_limit_usd": "19"}, {"tax_and_nonusage_reserve_usd": "0"},
                        {"funding_verified": False}, {"merchant_usage_limit_verified": False}):
            with self.subTest(changes=changes), self.assertRaises(subject.AcquisitionError):
                subject.validate_grant({**grant(plan), **changes}, plan, current_time=NOW)

    def test_grant_is_bound_to_manifest_and_short_expiry(self):
        plan = manifest()
        for changes in ({"manifest_sha256": "other"}, {"expires_at": NOW.isoformat()},
                        {"expires_at": (NOW + timedelta(days=2)).isoformat()},
                        {"account_verified_at": (NOW + timedelta(minutes=1)).isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(subject.AcquisitionError):
                subject.validate_grant({**grant(plan), **changes}, plan, current_time=NOW)

    def test_invalid_money_cannot_bypass_limits(self):
        for value in (True, float("nan"), float("inf"), -1, "0.0000001", None):
            with self.subTest(value=value), self.assertRaises(subject.AcquisitionError):
                subject._money(value)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "campaign"
        self.plan = manifest()
        self.grant = grant(self.plan)
        self.grant["campaign_directory"] = str(self.directory.resolve())
        self.factory = Factory(self.directory)
        patcher = patch.object(subject, "_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def execute(self, target="target-a"):
        return subject.execute_one(self.plan, self.grant, TOKEN, self.directory, target,
                                   client_factory=self.factory)

    def state(self):
        return json.loads((self.directory / "ledger.json").read_text())

    def test_success_stages_valid_rows_without_publish_and_never_starts_twice(self):
        result = self.execute()
        self.assertEqual((result["state"], result["eligible_count"]), ("downloaded", 1))
        self.assertEqual(self.execute(), result)
        self.assertEqual(self.factory.starts, 1)
        saved = json.loads((self.directory / "target-a.json").read_text())
        self.assertFalse(saved["coverage_complete"])
        self.assertEqual(saved["sales"][0]["price"], 1500000)
        self.assertEqual((self.directory / "ledger.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TOKEN, (self.directory / "ledger.json").read_text())
        self.assertEqual(self.state()["jobs"]["target-b"]["state"], "pending")

    def test_uncertain_start_blocks_all_new_runs(self):
        self.factory.crash_start = True
        with self.assertRaises(TimeoutError):
            self.execute()
        for target in ("target-a", "target-b"):
            with self.assertRaisesRegex(subject.AcquisitionError, "uncertain"):
                self.execute(target)
        self.assertEqual(self.factory.starts, 1)

    def test_known_uncertain_id_resumes_with_reads_only(self):
        self.factory.run_id_error = True
        with self.assertRaises(ApifyError):
            self.execute()
        self.factory.run_id_error = False
        self.assertEqual(self.execute()["state"], "downloaded")
        self.assertEqual(self.factory.starts, 1)

    def test_crashed_download_preserves_counters_and_full_byte_reservation(self):
        self.factory.crash_dataset = True
        with self.assertRaises(TimeoutError):
            self.execute()
        before = self.state()
        self.assertEqual(before["http_calls"], 5)
        self.assertEqual(before["reserved_bytes"], 4 * 100 + 2048)
        with self.assertRaisesRegex(subject.AcquisitionError, "existing active"):
            self.execute("target-b")
        self.factory.crash_dataset = False
        self.execute()
        self.assertGreater(self.state()["http_calls"], before["http_calls"])
        self.assertGreater(self.state()["reserved_bytes"], before["reserved_bytes"])
        self.assertEqual(self.factory.starts, 1)

    def test_timeout_is_terminal_without_resurrection(self):
        self.factory.status = "TIMED-OUT"
        result = self.execute()
        self.assertEqual(result["state"], "stopped")
        self.execute()
        self.assertEqual(self.factory.starts, 1)
        self.assertFalse(any("resurrect" in p for _, p in self.factory.requests))

    def test_changed_plan_grant_or_credential_never_resets_ledger(self):
        self.execute()
        before = (self.directory / "ledger.json").read_bytes()
        changed = deepcopy(self.plan)
        changed["targets"][0]["days"] = "24m"
        changed_plan_grant = {**grant(changed), "campaign_directory": self.grant["campaign_directory"]}
        changed_grant = {**self.grant, "cash_limit_usd": "9"}
        for p, g, token in ((changed, changed_plan_grant, TOKEN),
                            (self.plan, changed_grant, TOKEN),
                            (self.plan, self.grant, TOKEN + "new")):
            with self.subTest(), self.assertRaises(subject.AcquisitionError):
                subject.execute_one(p, g, token, self.directory, "target-b", client_factory=self.factory)
        self.assertEqual(before, (self.directory / "ledger.json").read_bytes())
        self.assertEqual(self.factory.starts, 1)

    def test_same_grant_cannot_start_again_in_a_different_directory(self):
        self.execute()
        other = Path(self.temp.name) / "different-folder"
        factory = Factory(other)
        with self.assertRaisesRegex(subject.AcquisitionError, "different campaign directory"):
            subject.execute_one(self.plan, self.grant, TOKEN, other, "target-a", client_factory=factory)
        self.assertFalse(other.exists(), "Reject before creating an alternate ledger")
        self.assertEqual(factory.starts, 0)
        self.assertEqual(self.factory.starts, 1)
        self.assertEqual(self.execute()["state"], "downloaded")

    def test_missing_or_noncanonical_grant_directory_is_rejected(self):
        for value in (None, "relative/campaign", str(self.directory.resolve()) + "/../campaign"):
            self.grant["campaign_directory"] = value
            with self.subTest(value=value), self.assertRaises(subject.AcquisitionError):
                self.execute()
        self.assertFalse(self.directory.exists())
        self.assertFalse(self.factory.requests)

    def test_missing_or_corrupt_ledger_does_not_reset_campaign(self):
        self.execute()
        path = self.directory / "ledger.json"
        path.write_text("not-json")
        with self.assertRaises(subject.AcquisitionError):
            self.execute()
        path.unlink()
        with self.assertRaises(subject.AcquisitionError):
            self.execute()

    def test_missing_download_never_claims_completion_or_restarts(self):
        self.execute()
        (self.directory / "target-a.json").unlink()
        with self.assertRaisesRegex(subject.AcquisitionError, "missing or changed"):
            self.execute()
        self.assertEqual(self.factory.starts, 1)

    def test_expiry_before_request_preserves_no_spend(self):
        self.grant["expires_at"] = NOW.isoformat()
        with self.assertRaises(subject.AcquisitionError):
            self.execute()
        self.assertFalse(self.directory.exists())
        self.assertFalse(self.factory.requests)


class PricingTests(unittest.TestCase):
    def test_pricing_change_or_separate_platform_usage_stops_before_run(self):
        pricing = {"pricingModel": "PAY_PER_EVENT", "startedAt": "2026-10-01T00:00:00Z",
                   "pricingPerEvent": {"actorChargeEvents": {
                       "listing": {"eventPriceUsd": .00085}, "property-details": {"eventPriceUsd": .0016}}}}
        for changes in ({}, {"isPPEPlatformUsagePaidByUser": True}, {"pricingModel": "PAY_PER_RESULT"}):
            info = {**pricing, **changes}
            queue = Queue(Response({"data": {"pricingInfos": [info]}}))
            client = subject.TargetClient(TOKEN, max_total_charge_usd=.245, opener=queue)
            with patch.object(subject, "_now", return_value=NOW):
                if changes:
                    with self.assertRaises(subject.AcquisitionError):
                        client.verify_pricing()
                else:
                    client.verify_pricing()
            self.assertEqual(len(queue.calls), 1)
            self.assertEqual(queue.calls[0].method, "GET")

    def test_price_increase_or_extra_event_is_rejected(self):
        for events in ({"listing": {"eventPriceUsd": .00086}, "property-details": {"eventPriceUsd": .0016}},
                       {"listing": {"eventPriceUsd": .00085}, "property-details": {"eventPriceUsd": .0016},
                        "actor-start": {"eventPriceUsd": .01}}):
            info = {"pricingModel": "PAY_PER_EVENT", "startedAt": "2026-10-01T00:00:00Z",
                    "pricingPerEvent": {"actorChargeEvents": events}}
            queue = Queue(Response({"data": {"pricingInfos": [info]}}))
            client = subject.TargetClient(TOKEN, max_total_charge_usd=.245, opener=queue)
            with patch.object(subject, "_now", return_value=NOW), self.assertRaises(subject.AcquisitionError):
                client.verify_pricing()
            self.assertEqual(len(queue.calls), 1)


if __name__ == "__main__":
    unittest.main()
