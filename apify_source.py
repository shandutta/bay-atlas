"""Bounded Zillow sold-data adapter for Apify's topapi/zillow-scraper.

Provider documentation, checked 2026-10-29:
https://apify.com/topapi/zillow-scraper/input-schema
https://docs.apify.com/api/v2/actors-runs-post
https://docs.apify.com/api/v2/actor-run-get
https://docs.apify.com/api/v2/dataset-get
https://docs.apify.com/api/v2/dataset-items-get

County searches include unincorporated places, unlike a list of cities. The
provider accepts sold-within 24m and geographically subdivides dense searches.
That describes requested coverage, not a guarantee that Zillow contains every
sale. A successful run or fully downloaded dataset is not proof of coverage.

No import-time network, retries, credential persistence, logging, or publishing.
The caller must durably reserve the run cap before start(), checkpoint the
returned ID, and resume that ID rather than start again after uncertain results.
maxTotalChargeUsd caps the ACTOR RUN. Subsequent dataset reads/transfer/storage
are separate platform usage; the caller must budget them and verify the account
plan. This client bounds calls and downloaded bytes, not all account spending.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from model import finite_number, prepare_sales, today_utc

API_BASE = "https://api.apify.com"
ACTOR = "topapi~zillow-scraper"
PROVIDER = "Zillow via Apify"
BAY_COUNTIES = tuple(name + " County, CA" for name in (
    "Alameda", "Contra Costa", "Marin", "Napa", "San Francisco",
    "San Mateo", "Santa Clara", "Solano", "Sonoma"))
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_METADATA_RESPONSE_BYTES = 256 * 1024
MAX_TOTAL_RESPONSE_BYTES = 64 * 1024 * 1024
BUDGET_NOTE = ("The provider caps actor-run charges. Dataset reads, transfer and "
               "retained storage are separate platform usage. Local HTTP and byte "
               "limits do not cap spending by other users of this account.")
ITEM_FIELDS = ",".join(("zpid", "propertyUrl", "listingStatus", "listingSoldPrice",
    "dateSold", "listingPriceHistory", "lastSoldPrice", "lastSoldDate", "detailsSource",
    "listingAddress", "coordinates", "homeType", "bedrooms",
    "livingArea", "resoFacts", "isAttached", "isDetached", "is_attached",
    "is_detached", "hasAttachedProperty", "propertySubType", "propertySubtype", "isValid", "searchInput"))
_STATES = {"READY", "RUNNING", "SUCCEEDED", "FAILED", "TIMING-OUT", "TIMED-OUT",
           "ABORTING", "ABORTED"}
_FAILURE_STATES = {"FAILED", "TIMED-OUT", "ABORTED"}


class ApifyError(RuntimeError):
    """Safe, fixed text; provider bodies and credential-bearing URLs are omitted."""
    def __init__(self, code, message, *, run_id=None):
        super().__init__(message)
        self.code = code
        self.run_id = run_id


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{5,64}", value):
        raise ApifyError("invalid_id", "The provider returned an invalid resource ID.")
    return value


def _integer(value, minimum=0, maximum=100000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid bounded integer")
    return value


def build_input(locations=BAY_COUNTIES, *, max_results=100000, include_details=False):
    """Request counties including rural areas; optional details add sold history.

    Details are separately charged by the actor, within the same run charge cap.
    The caller must reserve the cap before starting either input variant.
    """
    if not isinstance(locations, (list, tuple)) or not 1 <= len(locations) <= 20:
        raise ValueError("Supply one to twenty California locations.")
    if any(not isinstance(place, str) or len(place) > 120 or not place.endswith(", CA")
           or any(ord(c) < 32 for c in place) for place in locations):
        raise ValueError("Supply California place names ending in ', CA'.")
    _integer(max_results, 1)
    if not isinstance(include_details, bool):
        raise ValueError("include_details must be a boolean.")
    return {"searchLocation": list(dict.fromkeys(locations)), "listingStatus": "sold",
            "daysOnZillow": "24m", "homeTypes": ["house"], "minBeds": 2,
            "minSqft": 2000, "maxSqft": 3000, "maxResults": max_results,
            "maxResultsPerSearch": 0, "extractionMethod": "PAGINATION_WITH_ZOOM_IN",
            "compact": False, "flatOutput": False, "includeDetails": include_details,
            "monitorMode": False}


def _sold_date(value):
    # Provider documents a string. Do not turn days-on-market or scrape time into
    # a sale date. ISO timestamp calendar date is retained as supplied by source.
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", value):
        raise ValueError("Missing explicit sold date")
    if "T" in value:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.fromisoformat(value[:10]).isoformat()


def _sold_price(record, sold):
    """Prefer the explicit latest Sold event, never borrow an older sale's price.

    Zillow's compact listingSoldPrice amount can be rounded (for example,
    2,630,000 displayed for an actual 2,633,000 sale). The detail history retains
    exact amounts. Standalone lastSoldPrice has no usable date association.
    """
    history = record.get("listingPriceHistory")
    if history is None:
        history = []
    if not isinstance(history, list):
        raise ValueError("Invalid sale history")
    matched = set()
    for event in history:
        if not isinstance(event, dict):
            raise ValueError("Invalid sale history")
        if str(event.get("event", "")).strip().casefold() != "sold" or event.get("postingIsRental") is True:
            continue
        event_date = _sold_date(event.get("date"))
        if event_date > sold:
            raise ValueError("Sale history is newer than dateSold")
        if event_date == sold:
            amount = finite_number(event.get("price"))
            if amount <= 0:
                raise ValueError("Invalid matching sale price")
            matched.add(amount)
    if len(matched) > 1:
        raise ValueError("Conflicting sale prices on dateSold")

    paired_price = None
    if record.get("lastSoldDate") is not None:
        paired_date = _sold_date(record["lastSoldDate"])
        if paired_date > sold:
            raise ValueError("Last sale is newer than dateSold")
        if paired_date == sold:
            paired_price = finite_number(record.get("lastSoldPrice"))
            if paired_price <= 0:
                raise ValueError("Invalid paired sale price")
    if matched:
        price = matched.pop()
        if paired_price is not None and paired_price != price:
            raise ValueError("Conflicting dated sale evidence")
        return price, "Apify Zillow listingPriceHistory; exact Sold event price matching dateSold"
    if paired_price is not None:
        return paired_price, "Apify Zillow lastSoldPrice; paired lastSoldDate matches dateSold"
    price = finite_number(record["listingSoldPrice"].get("amount"))
    if price <= 0:
        raise ValueError("Invalid displayed sold price")
    return price, "Apify Zillow listingSoldPrice display amount; may be rounded"


def _attachment_evidence(record, facts):
    """Return fixed-text evidence only for an explicit attached-property signal."""
    for name, source in (("record", record), ("resoFacts", facts)):
        for flag in ("isAttached", "is_attached", "hasAttachedProperty"):
            if source.get(flag) is True:
                return "Zillow " + name + "." + flag + " true"
        for flag in ("isDetached", "is_detached"):
            if source.get(flag) is False:
                return "Zillow " + name + "." + flag + " false"
        subtype = str(source.get("propertySubType", source.get("propertySubtype", "")))
        if re.search(r"\b(attached|townhouse|townhome|condo|condominium|apartment)\b", subtype, re.I):
            return "Zillow " + name + " property subtype explicitly indicates an attached home"
    return None


def _normalize(record, as_of, *, allow_explicit_attached=False):
    if not isinstance(record, dict):
        return None, "invalid_record"
    if record.get("isValid") is False:
        return None, "provider_invalid"
    if record.get("listingStatus") not in {"sold", "SOLD", "RECENTLY_SOLD", "recentlySold"}:
        return None, "not_sold"
    zpid = str(record.get("zpid", ""))
    if not zpid.isdigit() or len(zpid) > 30:
        return None, "invalid_identity"
    source_url = record.get("propertyUrl")
    try:
        parsed = urlparse(source_url)
        matched = re.search(r"/(\d+)_zpid/?$", parsed.path)
        if (not isinstance(source_url, str) or parsed.scheme != "https"
                or parsed.hostname not in {"zillow.com", "www.zillow.com"}
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or not parsed.path.startswith("/homedetails/") or not matched
                or matched.group(1) != zpid or "\\" in source_url
                or any(ord(c) < 32 for c in source_url)):
            return None, "invalid_source"
    except (ValueError, TypeError, AttributeError):
        return None, "invalid_source"
    if record.get("homeType") != "SINGLE_FAMILY":
        return None, "not_single_family"
    facts = record.get("resoFacts")
    if facts is None:
        facts = {}
    if not isinstance(facts, dict):
        return None, "invalid_property_facts"
    if _attachment_evidence(record, facts) and not allow_explicit_attached:
        return None, "attached_home"
    price_obj, address, coords = (record.get(key) for key in
                                  ("listingSoldPrice", "listingAddress", "coordinates"))
    if not all(isinstance(value, dict) for value in (price_obj, address, coords)):
        return None, "missing_sold_fields"
    if price_obj.get("currency") != "USD":
        return None, "invalid_currency"
    if address.get("state") != "CA":
        return None, "outside_california"
    street, city = address.get("street"), address.get("city")
    if not isinstance(street, str) or not street.strip() or not isinstance(city, str) or not city.strip():
        return None, "missing_address"
    # Explicit unit data or unit-bearing street names contradict detached SFH.
    if address.get("unit") or re.search(r"\b(?:APT|UNIT|STE|SUITE)\s+\w|#\s*\w", street, re.I):
        return None, "attached_home"
    try:
        sold = _sold_date(record.get("dateSold"))
        price, price_evidence = _sold_price(record, sold)
        lat = finite_number(coords.get("latitude"))
        lng = finite_number(coords.get("longitude"))
        sqft = finite_number(record.get("livingArea"))
        beds = finite_number(record.get("bedrooms"))
    except (ValueError, TypeError, OverflowError):
        return None, "invalid_sold_fields"
    row = {"id": zpid, "source_id": zpid, "source_url": source_url,
           "street_address": street.strip(), "city": city.strip(),
           "address": ", ".join(str(v) for v in (street.strip(), city.strip(), "CA", address.get("zipCode")) if v),
           "lat": lat, "lng": lng, "price": price, "sqft": sqft, "beds": beds,
           "sold_date": sold, "sale_status": "sold", "home_type": "singleFamily",
           "date_evidence": "Apify Zillow dateSold; explicit sold date",
           "price_evidence": price_evidence,
           "property_type_evidence": "Zillow homeType SINGLE_FAMILY",
           "detachment_evidence": ("Zillow hasAttachedProperty false; single-family classification"
               if any(source.get("hasAttachedProperty") is False for source in (record, facts))
               else "Single-family classification; no explicit attached flag or unit")}
    valid, diagnostics = prepare_sales([row], as_of=as_of)
    if not valid:
        return None, next(iter(diagnostics["excluded"]), "invalid_record")
    return valid[0], None


def normalize_property(record, *, as_of=None):
    """Return one eligible sale or None; never fall back to listingPrice/Zestimate."""
    return _normalize(record, as_of or today_utc())[0]


def normalize_records(items, *, as_of=None):
    if not isinstance(items, list):
        raise ApifyError("invalid_dataset", "The provider dataset is not a list.")
    as_of = as_of or today_utc()
    excluded, candidates, attached_exclusions = Counter(), [], {}
    for record in items:
        row, reason = _normalize(record, as_of)
        if row:
            candidates.append(row)
        else:
            excluded[reason] += 1
            if reason == "attached_home":
                facts = record.get("resoFacts") or {}
                evidence = _attachment_evidence(record, facts)
                # An exclusion may remove an older saved comp, so require the
                # same complete source identity and eligible sold record checks.
                # Unit text alone and missing/invalid fields never authorize it.
                if evidence:
                    verified, _ = _normalize(record, as_of, allow_explicit_attached=True)
                    if verified:
                        fields = ("id", "source_id", "source_url", "street_address", "city",
                                  "address", "lat", "lng", "sold_date")
                        attached_exclusions[verified["source_id"]] = {
                            **{key: verified[key] for key in fields},
                            "reason": "explicit_attached_home", "attachment_evidence": evidence}
    sales, diagnostics = prepare_sales(candidates, as_of=as_of)
    excluded.update(diagnostics["excluded"])
    return {"sales": sales, "diagnostics": {"input_count": len(items),
            "eligible_count": len(sales), "excluded": dict(excluded),
            "explicit_attached_exclusions": list(attached_exclusions.values())}}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Bearer credentials, even to another Apify hostname.
        raise ApifyError("redirect", "The data API unexpectedly redirected the request.")


class ApifyClient:
    def __init__(self, api_key, *, max_total_charge_usd, max_http_calls=180,
                 max_dataset_items=100000, request_timeout=30, poll_interval=5,
                 wait_timeout=600, max_total_response_bytes=MAX_TOTAL_RESPONSE_BYTES,
                 opener=None, sleep=None, clock=None, before_request=None,
                 reserve_response=None, settle_response=None):
        if not isinstance(api_key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{16,256}", api_key):
            raise ApifyError("unconfigured", "Configure a valid Apify API token.")
        try:
            cap = finite_number(max_total_charge_usd)
            if not 0 < cap <= 100:
                raise ValueError()
            _integer(max_http_calls, 1, 1000)
            _integer(max_dataset_items, 1)
            _integer(max_total_response_bytes, 1024, 256 * 1024 * 1024)
            for value, maximum in ((request_timeout, 60), (poll_interval, 300), (wait_timeout, 21600)):
                if not 0 < finite_number(value) <= maximum:
                    raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise ApifyError("invalid_budget", "The API limits are outside the supported range.") from None
        self._key = api_key
        self.max_total_charge_usd = cap
        self.max_http_calls, self.max_dataset_items = max_http_calls, max_dataset_items
        self.request_timeout, self.poll_interval, self.wait_timeout = request_timeout, poll_interval, wait_timeout
        self.max_total_response_bytes = max_total_response_bytes
        self._open = opener or build_opener(_NoRedirect()).open
        self._sleep, self._clock = sleep or time.sleep, clock or time.monotonic
        self.before_request = before_request
        # The caller reserves the largest possible body before dispatch and
        # settles to the observed size only after a complete read. A crash or
        # partial/failed read therefore retains its reservation across resume.
        self.reserve_response, self.settle_response = reserve_response, settle_response
        self.http_calls, self.read_bytes = 0, 0
        self.budget_bytes = 0

    def _request(self, method, path, *, query=None, payload=None):
        # Paths are assembled locally; no provider URL or signed URL is followed.
        if (not isinstance(path, str) or not path.startswith("/v2/") or ".." in path
                or "?" in path or "#" in path or not re.fullmatch(r"/[A-Za-z0-9_~/.-]+", path)):
            raise ApifyError("unsafe_url", "The data API path was rejected.")
        if self.http_calls >= self.max_http_calls:
            raise ApifyError("http_limit", "The local API request limit was reached.")
        remaining = self.max_total_response_bytes - self.budget_bytes
        if remaining <= 1:
            raise ApifyError("response_limit", "The local download limit was reached.")
        response_maximum = MAX_RESPONSE_BYTES if path.endswith("/items") else MAX_METADATA_RESPONSE_BYTES
        # Include the overflow-detection byte in the reservation; even a body
        # without Content-Length cannot read beyond the durable allowance.
        limit = min(response_maximum, remaining - 1)
        reserved = limit + 1
        if self.before_request:
            self.before_request(method, path)
        if self.reserve_response:
            self.reserve_response(reserved)
        self.http_calls += 1
        self.budget_bytes += reserved
        url = API_BASE + path + (("?" + urlencode(query)) if query else "")
        request = Request(url, data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
                          method=method, headers={"Authorization": "Bearer " + self._key,
                          "Content-Type": "application/json", "Accept": "application/json",
                          "Accept-Encoding": "identity", "User-Agent": "BayAtlas/1.0"})
        try:
            with self._open(request, timeout=self.request_timeout) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                final = urlparse(final_url)
                if final.scheme != "https" or final.hostname != "api.apify.com" or final.username or final.password or final.port not in (None, 443):
                    raise ApifyError("unsafe_url", "The data API returned an unexpected destination.")
                headers = {str(k).lower(): str(v) for k, v in getattr(response, "headers", {}).items()}
                length = headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) > limit):
                    raise ApifyError("response_limit", "The data API response exceeds the download limit.")
                body = response.read(limit + 1)
                self.read_bytes += len(body)
                if self.settle_response:
                    self.settle_response(reserved, len(body))
                self.budget_bytes -= reserved - len(body)
                if len(body) > limit:
                    raise ApifyError("response_limit", "The data API response exceeds the download limit.")
            value = json.loads(body)
        except HTTPError as error:
            code = error.code
            if code in (401, 403):
                raise ApifyError("authentication", "Apify refused the API token or account access.") from None
            if code in (402, 429):
                raise ApifyError("provider_limit", "Apify reported an account, credit or rate limit.") from None
            raise ApifyError("http_error", "Apify could not complete the API request.") from None
        except (URLError, TimeoutError, OSError):
            # POST outcomes can be uncertain: caller must not automatically retry.
            raise ApifyError("network", "The data API request did not complete; its outcome may be uncertain.") from None
        except (ValueError, UnicodeError):
            raise ApifyError("invalid_json", "Apify returned an unreadable response.") from None
        if isinstance(value, dict) and value.get("error"):
            raise ApifyError("provider_error", "Apify returned a provider error.")
        return value, headers

    def _run(self, value, expected_id=None):
        raw = value.get("data") if isinstance(value, dict) else None
        if not isinstance(raw, dict):
            raise ApifyError("invalid_run", "Apify returned an invalid run record.")
        run_id = _identifier(raw.get("id"))
        if expected_id and run_id != expected_id:
            raise ApifyError("invalid_run", "The API run ID does not match the requested run.", run_id=expected_id)
        status = raw.get("status")
        if status not in _STATES:
            raise ApifyError("invalid_run", "Apify returned an unknown run state.", run_id=run_id)
        options = raw.get("options") or {}
        try:
            reported_cap = finite_number(options.get("maxTotalChargeUsd"))
            if not 0 < reported_cap <= self.max_total_charge_usd + 1e-9:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ApifyError("unconfirmed_cap", "Apify did not confirm the requested run cost limit.", run_id=run_id) from None
        result = {"id": run_id, "status": status, "max_total_charge_usd": reported_cap}
        if type(options.get("timeoutSecs")) is int and options["timeoutSecs"] > 0:
            result["timeout_seconds"] = options["timeoutSecs"]
        if type(options.get("restartOnError")) is bool:
            result["restart_on_error"] = options["restartOnError"]
        stats = raw.get("stats")
        if isinstance(stats, dict) and type(stats.get("resurrectCount")) is int and stats["resurrectCount"] >= 0:
            result["resurrect_count"] = stats["resurrectCount"]
        for field in ("defaultDatasetId", "actId"):
            if raw.get(field):
                result[field] = _identifier(raw[field])
        result["exitCode"] = raw.get("exitCode") if isinstance(raw.get("exitCode"), int) else None
        for field in ("usageTotalUsd", "chargedTotalUsd"):
            try:
                number = finite_number(raw.get(field))
                if number >= 0:
                    result[field] = number
            except (TypeError, ValueError):
                pass
        # Do not return arbitrary provider status text (which may echo input).
        message = str(raw.get("statusMessage", "")).casefold()
        result["cap_reported"] = any(term in message for term in (
            "maximum charge", "maximum total charge", "max charge", "max total charge",
            "maxtotalchargeusd", "charge limit", "budget limit", "maximum cost",
            "max results", "results limit", "result limit"))
        if status == "SUCCEEDED" and result["exitCode"] != 0:
            raise ApifyError("invalid_run", "Apify reported success without a successful exit code.", run_id=run_id)
        return result

    def start(self, input_dict):
        # Restrict this client to the documented safe sale-search contract.
        if not isinstance(input_dict, dict):
            raise ValueError("Expected the result of build_input().")
        expected = build_input(input_dict.get("searchLocation"), max_results=input_dict.get("maxResults"),
                               include_details=input_dict.get("includeDetails"))
        if input_dict != expected:
            raise ValueError("Expected the unmodified result of build_input().")
        value, _ = self._request("POST", "/v2/acts/" + ACTOR + "/runs", payload=input_dict,
            query={"maxTotalChargeUsd": format(self.max_total_charge_usd, ".8f"),
                   "timeout": int(self.wait_timeout), "restartOnError": "false"})
        run = self._run(value)
        if (run.get("timeout_seconds") != int(self.wait_timeout)
                or run.get("restart_on_error") is not False):
            raise ApifyError("unconfirmed_timeout", "Apify did not confirm the bounded actor timeout and disabled automatic restart.", run_id=run["id"])
        return run

    def resurrect_timeout(self, run, *, timeout_seconds):
        """One caller-checkpointed mutation of an existing timed-out run.

        The caller must persist its intent first and reconcile an uncertain POST
        via GET, never repeat it. Apify documents that maxTotalChargeUsd applies
        to the entire run including costs before resurrection:
        https://docs.apify.com/actors/running/runs-and-builds#resurrect-a-run-that-reached-its-cost-limit
        """
        _integer(timeout_seconds, 1, 21600)
        if (not isinstance(run, dict) or run.get("status") != "TIMED-OUT"
                or run.get("resurrect_count") != 0
                or run.get("max_total_charge_usd") != self.max_total_charge_usd):
            raise ApifyError("unsafe_recovery", "Only an unrecovered timed-out run with its original cost limit can be resumed.")
        run_id = _identifier(run.get("id"))
        dataset_id = _identifier(run.get("defaultDatasetId"))
        actor_id = _identifier(run.get("actId"))
        value, _ = self._request("POST", "/v2/actor-runs/" + run_id + "/resurrect",
            query={"timeout": timeout_seconds, "maxTotalChargeUsd": format(self.max_total_charge_usd, ".8f"),
                   "restartOnError": "false"})
        resumed = self._run(value, run_id)
        if (resumed.get("defaultDatasetId") != dataset_id or resumed.get("actId") != actor_id
                or resumed.get("max_total_charge_usd") != run["max_total_charge_usd"]
                or resumed.get("timeout_seconds") != timeout_seconds
                or resumed.get("restart_on_error") is not False):
            raise ApifyError("unconfirmed_recovery", "The resumed run identity or unchanged limits could not be verified; inspect its saved ID.", run_id=run_id)
        return resumed

    def get_run(self, run_id):
        run_id = _identifier(run_id)
        value, _ = self._request("GET", "/v2/actor-runs/" + run_id)
        return self._run(value, run_id)

    def wait(self, run_id):
        run_id = _identifier(run_id)
        deadline = self._clock() + self.wait_timeout
        while True:
            run = self.get_run(run_id)
            if run["status"] in _FAILURE_STATES:
                raise ApifyError("run_" + run["status"].lower().replace("-", "_"),
                                 "The provider run did not finish successfully.", run_id=run_id)
            if run["status"] == "SUCCEEDED":
                # Official docs: run costs/stats need about 10s to settle; dataset
                # itemCount may lag by 5s. Re-read once, never start a replacement.
                self._sleep(10)
                final = self.get_run(run_id)
                if final["status"] != "SUCCEEDED":
                    raise ApifyError("unstable_run", "The completed provider run changed state.", run_id=run_id)
                final["costs_preliminary"] = False
                return final
            if self._clock() >= deadline:
                raise ApifyError("wait_timeout", "The provider run is still pending; resume its saved ID.", run_id=run_id)
            self._sleep(min(self.poll_interval, max(0, deadline - self._clock())))

    def dataset(self, run, *, page_size=500, max_results=None):
        if not isinstance(run, dict) or run.get("status") != "SUCCEEDED" or run.get("exitCode") != 0:
            raise ApifyError("run_not_successful", "Only a successful provider run may be imported.")
        _integer(page_size, 1, 1000)
        if max_results is not None:
            _integer(max_results, 1)
        run_id, dataset_id = _identifier(run.get("id")), _identifier(run.get("defaultDatasetId"))

        def metadata():
            value, _ = self._request("GET", "/v2/datasets/" + dataset_id)
            raw = value.get("data") if isinstance(value, dict) else None
            try:
                if not isinstance(raw, dict) or raw.get("id") != dataset_id:
                    raise ValueError()
                count = _integer(raw.get("itemCount"), 0, self.max_dataset_items)
                if raw.get("actRunId") not in (None, run_id) or (run.get("actId") and raw.get("actId") not in (None, run["actId"])):
                    raise ValueError()
            except ValueError:
                raise ApifyError("invalid_dataset", "The provider dataset identity or size could not be verified.", run_id=run_id) from None
            return {"id": dataset_id, "itemCount": count, "modifiedAt": raw.get("modifiedAt")}

        initial = metadata()
        total, items, pages = initial["itemCount"], [], 0
        while True:
            offset = len(items)
            batch, headers = self._request("GET", "/v2/datasets/" + dataset_id + "/items",
                query={"format": "json", "offset": offset, "limit": page_size,
                       "desc": "false", "clean": "false", "skipEmpty": "false",
                       "skipHidden": "true", "fields": ITEM_FIELDS})
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise ApifyError("invalid_dataset", "Apify returned an invalid dataset page.", run_id=run_id)
            try:
                values = [int(headers["x-apify-pagination-" + key]) for key in ("offset", "limit", "count", "total")]
                if values != [offset, page_size, len(batch), total] or len(batch) > page_size:
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise ApifyError("pagination_mismatch", "Dataset pagination does not match its reported count.", run_id=run_id) from None
            items.extend(batch)
            pages += 1
            if len(items) > total or len(items) > self.max_dataset_items:
                raise ApifyError("dataset_limit", "The dataset exceeded its verified item limit.", run_id=run_id)
            if len(items) == total:
                break
            if not batch:
                raise ApifyError("pagination_mismatch", "The dataset ended before its reported count.", run_id=run_id)
        final = metadata()
        if final != initial:
            raise ApifyError("dataset_changed", "The dataset changed while it was being downloaded.", run_id=run_id)
        cap_reached = bool(run.get("cap_reported") or (max_results is not None and total >= max_results))
        cost = run.get("chargedTotalUsd", run.get("usageTotalUsd"))
        # Conservative proximity signal: detailed records cost $0.00245 each
        # (listing + details, checked 2026-10-29). A run may stop before the next
        # record would exceed its cap. This never establishes full coverage.
        if cost is not None and cost >= run.get("max_total_charge_usd", math.inf) - 0.00245:
            cap_reached = True
        return {"items": items, "item_count": total, "pages": pages, "dataset_id": dataset_id,
                "dataset_complete": True, "provider_run_succeeded": True,
                "cap_reached": cap_reached, "coverage_complete": False,
                "coverage_reason": "A completed dataset does not prove the provider exhausted every matching sale.",
                "http_calls": self.http_calls, "read_bytes": self.read_bytes,
                "budget_bytes": self.budget_bytes, "budget_note": BUDGET_NOTE}

    def fetch_sales(self, locations=BAY_COUNTIES, *, as_of=None, progress=None, run_id=None,
                    max_results=100000, include_details=False):
        if run_id is None:
            run_id = self.start(build_input(locations, max_results=max_results,
                                            include_details=include_details))["id"]
            if progress:
                progress({"run_id": run_id, "state": "started"})
        run = self.wait(run_id)
        downloaded = self.dataset(run, max_results=max_results)
        normalized = normalize_records(downloaded.pop("items"), as_of=as_of)
        return {**normalized, **downloaded, "run": run, "locations": list(locations)}
