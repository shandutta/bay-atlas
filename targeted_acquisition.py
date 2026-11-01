"""Explicit, one-job-at-a-time gap acquisitions with an independent spend ledger.

This operator tool never reads the web app's provider grant, publishes sales,
renews a subscription, or retries an uncertain start. It requires a new manifest
and separately verified one-time funding grant. See TARGETED_ACQUISITION.md.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re

from apify_source import ACTOR, ApifyClient, ApifyError, build_input, normalize_records
from refresh import atomic_json

VERSION = 1
BUILD = "0.1.23"
PRICE_MICRO_USD = {"listing": 850, "property-details": 1600}
MAX_ACTOR_MICRO_USD = 8_000_000
MAX_USAGE_MICRO_USD = 9_000_000
MAX_CASH_MICRO_USD = 10_000_000
MAX_HTTP_CALLS = 300
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 1800
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
RUN_ID = re.compile(r"[A-Za-z0-9]{5,64}")


class AcquisitionError(ValueError):
    """Fixed safe messages: never includes a credential or provider response."""


def _money(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        amount = Decimal(str(value)) * 1_000_000
        if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
            raise ValueError()
        return int(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise AcquisitionError("Money must be a nonnegative amount with at most six decimal places.") from None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _expiry(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise AcquisitionError("A one-time grant needs a timezone-aware expiry.") from None


def _now():
    return datetime.now(timezone.utc)


def _campaign_directory(value):
    """Bind this grant to one canonical ledger location, before any mutation."""
    try:
        if (not isinstance(value, str) or not value or len(value) > 4096
                or any(ord(c) < 32 for c in value)):
            raise ValueError()
        path = Path(value)
        resolved = path.resolve()
        if not path.is_absolute() or str(path) != str(resolved):
            raise ValueError()
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise AcquisitionError("The grant requires one absolute canonical campaign directory.") from None


def build_target_input(target):
    """Accept either one CA place or a small Bay Area rectangle, never a URL.

    All accepted records still pass the ordinary source and model validators.
    Search completion does not establish complete real-estate coverage.
    """
    if not isinstance(target, dict) or set(target) != {
            "id", "label", "search", "days", "max_results", "actor_cap_usd"}:
        raise AcquisitionError("A target must contain only the documented fields.")
    if not isinstance(target["id"], str) or not SAFE_ID.fullmatch(target["id"]):
        raise AcquisitionError("Invalid target identifier.")
    if (not isinstance(target["label"], str) or not 1 <= len(target["label"]) <= 120
            or any(ord(c) < 32 for c in target["label"])):
        raise AcquisitionError("Invalid target label.")
    if target["days"] not in {"30", "90", "6m", "12m", "24m"}:
        raise AcquisitionError("Targets must use a sale window of at most two years.")
    count = target["max_results"]
    if type(count) is not int or not 1 <= count <= 1000:
        raise AcquisitionError("Each target must request between 1 and 1000 results.")
    cap = _money(target["actor_cap_usd"])
    if not count * sum(PRICE_MICRO_USD.values()) <= cap <= 2_450_000:
        raise AcquisitionError("The target cap must cover its detailed rows and be at most $2.45.")
    search = target["search"]
    result = build_input(["California, CA"], max_results=count, include_details=True)
    if isinstance(search, dict) and set(search) == {"location"}:
        place = search["location"]
        # The source helper validates California suffix, length and controls.
        result = build_input([place], max_results=count, include_details=True)
    elif isinstance(search, dict) and set(search) == {"bounds"}:
        bounds = search["bounds"]
        if not isinstance(bounds, dict) or set(bounds) != {"north", "south", "east", "west"}:
            raise AcquisitionError("Map bounds require north, south, east and west.")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in bounds.values()):
            raise AcquisitionError("Map bounds must be finite coordinates.")
        n, s, e, w = (bounds[k] for k in ("north", "south", "east", "west"))
        if not (36.8 <= s < n <= 38.9 and -123.6 <= w < e <= -121.15
                and n - s <= .25 and e - w <= .25):
            raise AcquisitionError("Use a small rectangle within the Bay Area.")
        del result["searchLocation"]
        result["mapBounds"] = dict(bounds)
    else:
        raise AcquisitionError("A target needs one location or map bounds.")
    result.update(daysOnZillow=target["days"], sortBy="newest", maxConcurrency=2)
    return result


def validate_manifest(value):
    if not isinstance(value, dict) or set(value) != {
            "version", "campaign_id", "as_of", "overhead_reserve_usd", "targets"}:
        raise AcquisitionError("Invalid manifest fields.")
    if (type(value["version"]) is not int or value["version"] != VERSION or not isinstance(value["campaign_id"], str)
            or not SAFE_ID.fullmatch(value["campaign_id"])):
        raise AcquisitionError("Invalid manifest version or campaign identifier.")
    try:
        date.fromisoformat(value["as_of"])
    except (ValueError, TypeError):
        raise AcquisitionError("A manifest requires an ISO date.") from None
    targets = value["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise AcquisitionError("Supply between one and twenty targets.")
    for target in targets:
        build_target_input(target)
    if len({t["id"] for t in targets}) != len(targets):
        raise AcquisitionError("Target identifiers must be unique.")
    caps = sum(_money(t["actor_cap_usd"]) for t in targets)
    overhead = _money(value["overhead_reserve_usd"])
    if caps > MAX_ACTOR_MICRO_USD or overhead < 1_000_000 or caps + overhead > MAX_USAGE_MICRO_USD:
        raise AcquisitionError("The plan must fit $8 actor charges plus at least $1 overhead, at most $9 usage.")
    return {"campaign_id": value["campaign_id"], "targets": len(targets),
            "maximum_detailed_rows": sum(t["max_results"] for t in targets),
            "actor_caps_usd": caps / 1_000_000,
            "overhead_reserve_usd": overhead / 1_000_000,
            "usage_ceiling_usd": (caps + overhead) / 1_000_000,
            "manifest_sha256": _hash(value), "coverage_complete": False}


def validate_grant(grant, manifest, *, current_time=None, check_expiry=True):
    summary = validate_manifest(manifest)
    fields = {"version", "campaign_id", "manifest_sha256", "enabled", "expires_at",
              "cash_limit_usd", "fixed_cash_paid_usd", "available_usage_credit_usd",
              "tax_and_nonusage_reserve_usd", "merchant_usage_limit_verified",
              "funding_verified", "account_verified_at", "funding_kind", "campaign_directory"}
    if not isinstance(grant, dict) or set(grant) != fields:
        raise AcquisitionError("A new grant must contain exactly the documented fields.")
    if (type(grant["version"]) is not int or grant["version"] != VERSION or grant["campaign_id"] != manifest["campaign_id"]
            or grant["manifest_sha256"] != summary["manifest_sha256"]
            or grant["enabled"] is not True or grant["funding_verified"] is not True
            or grant["merchant_usage_limit_verified"] is not True
            or grant["funding_kind"] not in {"free_credit", "prepaid_token", "paid_account"}):
        raise AcquisitionError("Fresh funding and merchant-limit verification are required for this exact plan.")
    current_time = current_time or _now()
    expiry = _expiry(grant["expires_at"])
    checked = _expiry(grant["account_verified_at"])
    if checked > current_time or expiry <= checked or (expiry - checked).total_seconds() > 86400:
        raise AcquisitionError("Verification must precede a grant expiry within 24 hours.")
    if check_expiry and current_time >= expiry:
        raise AcquisitionError("The one-time grant expired; no new provider request is allowed.")
    cash, fixed, credit, extra = (_money(grant[k]) for k in (
        "cash_limit_usd", "fixed_cash_paid_usd", "available_usage_credit_usd",
        "tax_and_nonusage_reserve_usd"))
    usage = _money(summary["usage_ceiling_usd"])
    if (not 0 < cash <= MAX_CASH_MICRO_USD or fixed > cash or extra < 1_000_000
            or fixed + max(0, usage - credit) + extra > cash):
        raise AcquisitionError("Funding must fit the new $10 cash limit including fixed fees, uncovered usage and reserve.")
    if grant["funding_kind"] in {"free_credit", "prepaid_token"} and usage > credit:
        raise AcquisitionError("The plan exceeds the verified spendable credit.")
    _campaign_directory(grant["campaign_directory"])
    return summary


@contextmanager
def _locked(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise AcquisitionError("Use a dedicated private campaign directory.")
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise AcquisitionError("Another campaign operation is active.") from None
    finally:
        os.close(fd)


def _save(path, value):
    atomic_json(path, value, private=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _state(directory, manifest, grant, token):
    path = directory / "ledger.json"
    expected = {"version": VERSION, "manifest_sha256": _hash(manifest),
                "grant_sha256": _hash(grant), "credential_sha256": hashlib.sha256(token.encode()).hexdigest()}
    if path.exists():
        try:
            state = json.loads(path.read_text())
            if any(state.get(k) != v for k, v in expected.items()):
                raise ValueError()
            if (set(state["jobs"]) != {t["id"] for t in manifest["targets"]}
                    or any(type(state.get(k)) is not int or state[k] < 0 for k in ("http_calls", "reserved_bytes"))):
                raise ValueError()
            for job in state["jobs"].values():
                if not isinstance(job, dict) or job.get("state") not in {
                        "pending", "start_intent", "running", "stopped", "downloaded"}:
                    raise ValueError()
                if "run_id" in job and (not isinstance(job["run_id"], str) or not RUN_ID.fullmatch(job["run_id"])):
                    raise ValueError()
                if job["state"] not in {"pending", "start_intent"} and not job.get("run_id"):
                    raise ValueError()
                if job["state"] == "pending" and set(job) != {"state"}:
                    raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            raise AcquisitionError("The saved ledger is invalid or its immutable plan, grant or credential changed.") from None
        return state
    # Refuse any existing campaign outputs without their authoritative ledger.
    if any(p.name != ".lock" for p in directory.iterdir()):
        raise AcquisitionError("Campaign files exist without a ledger; reconcile them before continuing.")
    state = {**expected, "created_at": _now().isoformat(), "http_calls": 0, "reserved_bytes": 0,
             "usage_reserved_usd": validate_manifest(manifest)["usage_ceiling_usd"],
             "jobs": {t["id"]: {"state": "pending"} for t in manifest["targets"]}}
    _save(path, state)
    return state


class TargetClient(ApifyClient):
    def start_target(self, target):
        payload = build_target_input(target)
        value, _ = self._request("POST", "/v2/acts/" + ACTOR + "/runs", payload=payload,
            query={"build": BUILD, "maxTotalChargeUsd": format(self.max_total_charge_usd, ".8f"),
                   "timeout": int(self.wait_timeout), "restartOnError": "false"})
        run = self._run(value)
        if (run.get("timeout_seconds") != int(self.wait_timeout) or run.get("restart_on_error") is not False
                or _money(run["max_total_charge_usd"]) != _money(target["actor_cap_usd"])):
            raise ApifyError("unconfirmed_limits", "Run limits require reconciliation.", run_id=run["id"])
        return run

    def verify_pricing(self):
        value, _ = self._request("GET", "/v2/acts/" + ACTOR)
        try:
            infos = value["data"]["pricingInfos"]
            active = [p for p in infos if not p.get("endedAt") and
                      _expiry(p["startedAt"]) <= _now()]
            if len(active) != 1:
                raise ValueError()
            info = active[0]
            if info["pricingModel"] != "PAY_PER_EVENT" or info.get("isPPEPlatformUsagePaidByUser") is True:
                raise ValueError()
            events = info["pricingPerEvent"]["actorChargeEvents"]
            if set(events) != set(PRICE_MICRO_USD):
                raise ValueError()
            if any(_money(events[k]["eventPriceUsd"]) != v for k, v in PRICE_MICRO_USD.items()):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AcquisitionError("Current actor pricing differs from the reviewed rates; no run was started.") from None


def execute_one(manifest, grant, token, directory, target_id, *, client_factory=TargetClient):
    """Start once, or collect a known run; write only to its private campaign.

    A process interruption after start intent but before ID checkpoint requires
    manual reconciliation. Re-running this function cannot issue another start.
    All planned caps remain reserved, even if runs spend less or fail.
    """
    validate_grant(grant, manifest)
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{16,256}", token):
        raise AcquisitionError("Supply a valid credential separately from the grant.")
    targets = {t["id"]: t for t in manifest["targets"]}
    if target_id not in targets:
        raise AcquisitionError("Unknown target identifier.")
    target = targets[target_id]
    try:
        directory = Path(directory).resolve()
    except (OSError, ValueError, RuntimeError, TypeError):
        raise AcquisitionError("Invalid campaign directory.") from None
    if directory != _campaign_directory(grant["campaign_directory"]):
        raise AcquisitionError("This grant belongs to a different campaign directory; no state or request was created.")
    with _locked(directory):
        state = _state(directory, manifest, grant, token)
        path = directory / "ledger.json"
        job = state["jobs"][target_id]
        if job["state"] in {"downloaded", "stopped"}:
            if job["state"] == "downloaded":
                try:
                    expected = job["output_sha256"]
                    actual = hashlib.sha256((directory / (target_id + ".json")).read_bytes()).hexdigest()
                    if expected != actual:
                        raise ValueError()
                except (OSError, ValueError, KeyError):
                    raise AcquisitionError("The downloaded output is missing or changed; reconcile without another start.") from None
            return dict(job)
        uncertain = [j for j in state["jobs"].values() if j["state"] == "start_intent" and not j.get("run_id")]
        if uncertain:
            raise AcquisitionError("An uncertain start needs manual reconciliation; no other run may start.")
        active = [key for key, j in state["jobs"].items() if j["state"] in {"start_intent", "running"}]
        if any(key != target_id for key in active):
            raise AcquisitionError("Collect the existing active target before starting another.")

        def before_request(method, request_path):
            validate_grant(grant, manifest)
            if state["http_calls"] >= MAX_HTTP_CALLS:
                raise AcquisitionError("The campaign API-call allowance is exhausted.")
            state["http_calls"] += 1
            _save(path, state)

        def reserve(size):
            if state["reserved_bytes"] + size > MAX_DOWNLOAD_BYTES:
                raise AcquisitionError("The campaign download allowance is exhausted.")
            state["reserved_bytes"] += size
            _save(path, state)

        def settle(reserved, actual):
            state["reserved_bytes"] -= reserved - actual
            _save(path, state)

        timeout = min(MAX_TIMEOUT_SECONDS, int((_expiry(grant["expires_at"]) - _now()).total_seconds()) - 30)
        if timeout < 1:
            raise AcquisitionError("Too little time remains before the grant expires.")
        client = client_factory(token, max_total_charge_usd=float(target["actor_cap_usd"]),
            max_http_calls=max(1, MAX_HTTP_CALLS - state["http_calls"]), max_dataset_items=1000,
            wait_timeout=timeout, poll_interval=30,
            max_total_response_bytes=max(1024, MAX_DOWNLOAD_BYTES - state["reserved_bytes"]),
            before_request=before_request, reserve_response=reserve, settle_response=settle)
        if job["state"] == "pending":
            client.verify_pricing()
            job.update(state="start_intent", requested_at=_now().isoformat(), timeout_seconds=timeout)
            _save(path, state)
            try:
                run = client.start_target(target)
            except ApifyError as error:
                if error.run_id:
                    job["run_id"] = error.run_id
                    _save(path, state)
                raise
            job.update(state="running", run_id=run["id"])
            _save(path, state)
        run = client.get_run(job["run_id"])
        if (run.get("max_total_charge_usd") != float(target["actor_cap_usd"])
                or run.get("timeout_seconds") != job.get("timeout_seconds")
                or run.get("restart_on_error") is not False):
            raise AcquisitionError("Known run limits could not be confirmed; reconcile before further work.")
        if run["status"] in {"ABORTED", "FAILED", "TIMED-OUT"}:
            job.update(state="stopped", provider_status=run["status"])
            _save(path, state)
            return dict(job)
        run = client.wait(job["run_id"])
        downloaded = client.dataset(run, max_results=target["max_results"])
        normalized = normalize_records(downloaded.pop("items"), as_of=_now().date())
        output = {"campaign_id": manifest["campaign_id"], "target_id": target_id,
                  "run_id": run["id"], "as_of": _now().date().isoformat(),
                  **normalized, "download": downloaded, "coverage_complete": False}
        output_path = directory / (target_id + ".json")
        _save(output_path, output)
        job.update(state="downloaded", eligible_count=len(normalized["sales"]),
                   input_count=downloaded["item_count"], cap_reached=downloaded["cap_reached"],
                   output_sha256=hashlib.sha256(output_path.read_bytes()).hexdigest(),
                   actor_charge_usd=run.get("chargedTotalUsd", run.get("usageTotalUsd")),
                   finished_at=_now().isoformat())
        _save(path, state)
        return dict(job)
