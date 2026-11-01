"""Checkpointed, on-demand county refreshes using the documented Apify API.

The caller must hold RefreshManager.operation_lock. No schedule is installed.
Actor caps plus a conservative API allowance are reserved *before* dispatch and
never automatically released or reset. An uncertain start is never retried.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse
import uuid

from model import finite_number, in_bay_area, merge_sales, parse_date, source_identity, source_provider, today_utc
from refresh import atomic_json, now

BAY_COUNTIES = (
    'Alameda County, CA', 'Contra Costa County, CA', 'Marin County, CA',
    'Napa County, CA', 'San Francisco County, CA', 'San Mateo County, CA',
    'Santa Clara County, CA', 'Solano County, CA', 'Sonoma County, CA',
)
MAX_HTTP_CALLS = 180
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_RUN_TIMEOUT_SECONDS = 21600
MAX_POLL_INTERVAL_SECONDS = 300
PROVIDER = 'Zillow via Apify'
PRICING_URL = 'https://apify.com/pricing'
BUDGET_NOTE = ('Reservations are a conservative lifetime limit for this app, not an account-wide spending cap. '
               'Free refresh requires a verified free plan; paid refresh requires a separate expiring one-time allowance. '
               'API reads, transfer and retained dataset storage can also consume credits; merchant billing remains authoritative.')
PAID_MONEY_FIELDS = ('total_budget_usd', 'fixed_cash_paid_usd', 'included_usage_credit_usd',
                     'tax_nonusage_reserve_usd', 'usage_budget_usd')
PAID_RECORD_FIELDS = tuple(name.replace('_usd', '_micro_usd') for name in PAID_MONEY_FIELDS)


class ApifyRefreshError(RuntimeError):
    """Safe, user-facing error; provider response bodies are never echoed."""


def _money(value, name, *, allow_zero=False):
    try:
        if isinstance(value, bool):
            raise ValueError()
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or (not allow_zero and amount == 0):
            raise ValueError()
        return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise ApifyRefreshError('Invalid ' + name + '; refresh is paused to protect the credit budget.') from None


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ApifyRefreshError('Invalid ' + name + '; refresh is paused.')
    return value


def _paid_record_valid(record):
    if (not isinstance(record, dict)
            or not isinstance(record.get('id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', record['id'])
            or any(type(record.get(key)) is not int or record[key] < 0 for key in PAID_RECORD_FIELDS)):
        return False
    total, fixed, credit, extra, usage = (record[key] for key in PAID_RECORD_FIELDS)
    if not 0 < total <= 75_000_000 or not 0 < usage <= 100_000_000:
        return False
    if fixed > total or credit > 100_000_000 or extra < 1_000_000:
        return False
    if fixed + max(0, usage - credit) + extra > total:
        return False
    try:
        expiry = datetime.fromisoformat(record['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            return False
    except (KeyError, TypeError, AttributeError, ValueError):
        return False
    generation_id = record.get('generation_id')
    return generation_id is None or (isinstance(generation_id, str)
                                    and bool(re.fullmatch(r'[a-f0-9]{32}', generation_id)))


def _paid_allowance(cfg, *, check_expiry=True):
    """Validate an operator-installed grant; browser configuration cannot grant it.

    Included subscription/prepaid credit offsets usage, not tax or fixed cash
    already paid. This is a conservative local authorization calculation, not
    a substitute for the merchant's actual invoice or account spending limit.
    """
    value = cfg.get('apify_paid_allowance')
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ApifyRefreshError('The one-time paid allowance is invalid; no request was made.')
    if value.get('enabled') is not True:
        raise ApifyRefreshError('The one-time paid allowance is disabled. No paid or free fallback request was made.')
    if value.get('payment_verified') is not True or value.get('account_limit_verified') is not True:
        raise ApifyRefreshError('Verify the payment and merchant account usage limit before enabling the one-time import.')
    record = {key.replace('_usd', '_micro_usd'): _money(value.get(key), key, allow_zero=True)
              for key in PAID_MONEY_FIELDS}
    try:
        expiry = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError()
        record.update(id=value['id'], expires_at=expiry.astimezone(timezone.utc).isoformat())
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ApifyRefreshError('The one-time paid allowance must have an explicit expiry with a time zone.') from None
    if not _paid_record_valid(record):
        raise ApifyRefreshError('The one-time import must fit its approved total of at most $75, including fixed charges, uncovered usage, and at least $1 tax/nonusage headroom.')
    if check_expiry:
        _check_paid_expiry(record)
    return record


def _check_paid_expiry(record):
    if record and datetime.now(timezone.utc) >= datetime.fromisoformat(record['expires_at']):
        raise ApifyRefreshError('The one-time paid allowance has expired. No further paid API requests are authorized.')


def _bounded_timeout(seconds, paid):
    """Keep remote execution within this allowance, including dispatch headroom."""
    if paid:
        remaining = (datetime.fromisoformat(paid['expires_at']) - datetime.now(timezone.utc)).total_seconds()
        seconds = min(seconds, int(remaining) - 30)
        if seconds < 1:
            raise ApifyRefreshError('The one-time allowance has too little time left to safely start or resume an actor.')
    return seconds


def _recovery_record_valid(record, job):
    if not isinstance(record, dict) or record.get('phase') not in {'intent', 'confirmed'}:
        return False
    if (record.get('run_id') != job.get('run_id')
            or any(not isinstance(record.get(key), str) or not re.fullmatch(r'[A-Za-z0-9]{5,64}', record[key])
                   for key in ('dataset_id', 'actor_id'))
            or type(record.get('cap_micro_usd')) is not int
            or record['cap_micro_usd'] != job.get('actor_micro_usd')
            or type(record.get('prior_resurrect_count')) is not int or record['prior_resurrect_count'] != 0
            or type(record.get('prior_cost_micro_usd')) is not int
            or not 0 <= record['prior_cost_micro_usd'] < record['cap_micro_usd']
            or type(record.get('timeout_seconds')) is not int
            or not 1 <= record['timeout_seconds'] <= MAX_RUN_TIMEOUT_SECONDS):
        return False
    try:
        requested = datetime.fromisoformat(record['requested_at'])
        return requested.tzinfo is not None and requested.utcoffset() is not None
    except (ValueError, TypeError, KeyError):
        return False


def _confirm_recovery(run, record):
    """A GET can confirm a prior POST; it can never authorize another POST."""
    if (run.get('id') != record['run_id'] or run.get('defaultDatasetId') != record['dataset_id']
            or run.get('actId') != record['actor_id']
            or _money(run.get('max_total_charge_usd'), 'resumed actor cap') != record['cap_micro_usd']
            or run.get('resurrect_count') != record['prior_resurrect_count'] + 1
            or run.get('timeout_seconds') != record['timeout_seconds']
            or run.get('restart_on_error') is not False):
        raise ApifyRefreshError('The saved recovery needs review in Apify. Its outcome or unchanged limits are unconfirmed; no second recovery was sent.')


def _wait_with_recovery(client, job, state, root, paid, timeout_seconds):
    """Resume one timed-out paid run once; uncertainty only permits GET reads."""
    recovery = job.get('timeout_recovery')
    if job.get('requires_run_confirmation'):
        observed = client.get_run(job['run_id'])
        confirmed_timeout = observed.get('timeout_seconds')
        if (type(confirmed_timeout) is not int or not 0 < confirmed_timeout <= job['run_timeout_seconds']
                or observed.get('restart_on_error') is not False):
            raise ApifyRefreshError('The original actor timeout or restart settings still need review; no replacement run was started.')
        job.pop('requires_run_confirmation')
        job['state'] = 'running'
        _save(root, state)
    if recovery:
        _confirm_recovery(client.get_run(job['run_id']), recovery)
        recovery['phase'] = 'confirmed'
        job['state'] = 'running'
        _save(root, state)
    try:
        run = client.wait(job['run_id'])
    except Exception as error:
        if getattr(error, 'code', '') != 'run_timed_out' or not paid or recovery:
            raise
        original = client.get_run(job['run_id'])
        cost_values = [original[key] for key in ('usageTotalUsd', 'chargedTotalUsd') if key in original]
        if (original.get('status') != 'TIMED-OUT' or original.get('resurrect_count') != 0
                or _money(original.get('max_total_charge_usd'), 'actor cap') != job['actor_micro_usd']
                or not cost_values):
            raise ApifyRefreshError('The timed-out run or its original cumulative cost limit could not be verified; no recovery was sent.')
        cost = _money(max(cost_values), 'settled actor cost', allow_zero=True)
        if cost >= job['actor_micro_usd'] - 2450:
            job['state'] = 'failed'
            _save(root, state)
            raise ApifyRefreshError('The timed-out run already reached its reserved cost limit; no recovery was sent.')
        timeout = _bounded_timeout(timeout_seconds, paid)
        recovery = {'phase': 'intent', 'run_id': job['run_id'],
                    'dataset_id': original.get('defaultDatasetId'), 'actor_id': original.get('actId'),
                    'cap_micro_usd': job['actor_micro_usd'], 'prior_resurrect_count': 0,
                    'prior_cost_micro_usd': cost, 'timeout_seconds': timeout, 'requested_at': now()}
        if not _recovery_record_valid(recovery, job):
            raise ApifyRefreshError('The timed-out run storage identity could not be verified; no recovery was sent.')
        job['timeout_recovery'] = recovery
        _save(root, state)  # Durable intent before POST; never cleared/reissued.
        resumed = client.resurrect_timeout(original, timeout_seconds=timeout)
        _confirm_recovery(resumed, recovery)
        recovery['phase'] = 'confirmed'
        job['state'] = 'running'
        _save(root, state)
        run = client.wait(job['run_id'])
    if recovery:
        _confirm_recovery(run, recovery)
    return run


def _paths(root):
    data = Path(root) / 'data'
    return data, data / 'apify-state.json'


def _load(root):
    _, path = _paths(root)
    if not path.exists():
        return {'version': 1, 'reservations': [], 'generation': None, 'history': []}
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or state.get('version') != 1:
            raise ValueError()
        reservations = state['reservations']
        if not isinstance(reservations, list):
            raise ValueError()
        paid = state.get('paid_allowance')
        if paid is not None and not _paid_record_valid(paid):
            raise ValueError()
        ids, by_id = set(), {}
        for reservation in reservations:
            if (not isinstance(reservation, dict) or not isinstance(reservation.get('id'), str)
                    or reservation['id'] in ids or type(reservation.get('micro_usd')) is not int
                    or type(reservation.get('actor_micro_usd')) is not int
                    or type(reservation.get('api_micro_usd')) is not int
                    or reservation['actor_micro_usd'] <= 0 or reservation['api_micro_usd'] < 20_000
                    or reservation['micro_usd'] != reservation['actor_micro_usd'] + reservation['api_micro_usd']
                    or reservation.get('county') not in BAY_COUNTIES
                    or not isinstance(reservation.get('generation'), str)):
                raise ValueError()
            allowance_id = reservation.get('paid_allowance_id')
            if allowance_id is not None and (not paid or allowance_id != paid['id']
                    or reservation['api_micro_usd'] < 250_000
                    or reservation['generation'] != paid.get('generation_id')):
                raise ValueError()
            ids.add(reservation['id'])
            by_id[reservation['id']] = reservation
        if paid and (not paid.get('generation_id') or sum(item['micro_usd'] for item in reservations
                if item.get('paid_allowance_id') == paid['id']) > paid['usage_budget_micro_usd']):
            raise ValueError()
        generation = state['generation']
        if generation is not None:
            if not isinstance(generation, dict) or not isinstance(generation.get('id'), str):
                raise ValueError()
            if not generation['id'].isalnum() or len(generation['id']) != 32:
                raise ValueError()
            if type(generation.get('max_results')) is not int or not 1 <= generation['max_results'] <= 100000:
                raise ValueError()
            if not isinstance(generation.get('include_details', False), bool):
                raise ValueError()
            jobs = generation['jobs']
            if not isinstance(jobs, list) or [job['county'] for job in jobs] != list(BAY_COUNTIES):
                raise ValueError()
            used_reservations, used_run_ids = set(), set()
            for job in jobs:
                if job['state'] not in {'pending', 'starting', 'running', 'succeeded', 'failed', 'needs_review'}:
                    raise ValueError()
                if type(job.get('http_calls')) is not int or type(job.get('read_bytes')) is not int:
                    raise ValueError()
                if 'requires_run_confirmation' in job and type(job['requires_run_confirmation']) is not bool:
                    raise ValueError()
                for key, minimum, maximum in (('run_timeout_seconds', 1, MAX_RUN_TIMEOUT_SECONDS),
                                               ('poll_interval_seconds', 1, MAX_POLL_INTERVAL_SECONDS)):
                    if key in job and (type(job[key]) is not int or not minimum <= job[key] <= maximum):
                        raise ValueError()
                if not 0 <= job['http_calls'] <= MAX_HTTP_CALLS or not 0 <= job['read_bytes'] <= MAX_RESPONSE_BYTES:
                    raise ValueError()
                if job['state'] != 'pending' and job.get('reservation_id') not in ids:
                    raise ValueError()
                if job['state'] != 'pending':
                    reservation = by_id[job['reservation_id']]
                    if (job['reservation_id'] in used_reservations
                            or reservation['county'] != job['county']
                            or reservation['generation'] != generation['id']
                            or type(job.get('actor_micro_usd')) is not int
                            or job['actor_micro_usd'] != reservation['actor_micro_usd']):
                        raise ValueError()
                    used_reservations.add(job['reservation_id'])
                elif job.get('run_id') or job.get('reservation_id') or job['http_calls'] or job['read_bytes']:
                    raise ValueError()
                if job.get('run_id') is not None and (not isinstance(job['run_id'], str) or not job['run_id'].isalnum()):
                    raise ValueError()
                if job['state'] in {'running', 'succeeded', 'failed'} and not job.get('run_id'):
                    raise ValueError()
                if job.get('run_id'):
                    if job['run_id'] in used_run_ids:
                        raise ValueError()
                    used_run_ids.add(job['run_id'])
                recovery = job.get('timeout_recovery')
                if recovery is not None and (not paid or job['state'] in {'pending', 'starting'}
                        or not _recovery_record_valid(recovery, job)):
                    raise ValueError()
            checked = generation.get('publication_checked_run_ids', [])
            if (not isinstance(checked, list) or any(not isinstance(value, str) for value in checked)
                    or len(set(checked)) != len(checked)
                    or not set(checked).issubset({job['run_id'] for job in jobs if job['state'] == 'succeeded'})):
                raise ValueError()
        if not isinstance(state.get('history'), list):
            raise ValueError()
        return state
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ApifyRefreshError('The Apify credit or checkpoint log could not be read. Refresh is paused; the saved map is intact.') from None


def _sync_parents(path, root):
    """Persist new file names and any newly-created directories up to root."""
    directory, root = Path(path).parent, Path(root)
    while True:
        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if directory == root:
            break
        if directory.parent == directory:
            raise ApifyRefreshError('The checkpoint directory is outside the application root.')
        directory = directory.parent


def _save(root, state):
    atomic_json(_paths(root)[1], state, True)
    # atomic_json fsyncs contents; sync the rename before external dispatch.
    _sync_parents(_paths(root)[1], root)


def status(root):
    """Return safe progress and reserved cost; no key or raw provider response."""
    state = _load(root)
    jobs = (state.get('generation') or {}).get('jobs', [])
    reserved = sum(item['micro_usd'] for item in state['reservations'])
    generation = state.get('generation') or {}
    terminal = bool(jobs) and all(job['state'] in {'succeeded', 'failed'} for job in jobs)
    completed_runs = sorted(job['run_id'] for job in jobs if job['state'] == 'succeeded')
    publication_pending = bool(terminal and completed_runs
        and generation.get('publication_checked_run_ids') != completed_runs)
    if publication_pending:
        last = _read_dataset(_paths(root)[0] / 'sales.json').get('last_api_refresh') or {}
        if isinstance(last, dict) and last.get('generation') == generation.get('id'):
            ids = last.get('published_run_ids', [])
            published = set(ids) if isinstance(ids, list) and all(isinstance(value, str) for value in ids) else set()
            publication_pending = not set(completed_runs).issubset(published)
    return {
        'reserved_usd': reserved / 1_000_000,
        'credit_reserved_usd': reserved / 1_000_000,
        'budget_window': 'lifetime reservations; no automatic reset',
        'reservation_count': len(state['reservations']),
        'counties_completed': sum(job['state'] == 'succeeded' for job in jobs),
        'counties_complete': sum(job['state'] == 'succeeded' for job in jobs),
        'counties_total': len(BAY_COUNTIES),
        'http_calls': sum(job['http_calls'] for job in jobs),
        'response_bytes_reserved': sum(job['read_bytes'] for job in jobs),
        'paid_allowance': ({'expires_at': state['paid_allowance']['expires_at'],
            'usage_budget_usd': state['paid_allowance']['usage_budget_micro_usd'] / 1_000_000,
            'reserved_usd': sum(item['micro_usd'] for item in state['reservations']
                               if item.get('paid_allowance_id') == state['paid_allowance']['id']) / 1_000_000,
            'generation_finished': terminal, 'publication_pending': publication_pending}
            if state.get('paid_allowance') else None),
        'jobs': [{key: job[key] for key in ('county', 'state', 'http_calls')}
                 | {'eligible_count': job.get('eligible_count', 0)} for job in jobs],
        'note': BUDGET_NOTE,
        'paused_reason': ('An actor recovery needs review in Apify; only its saved run will be checked.'
                          if any((job.get('timeout_recovery') or {}).get('phase') == 'intent' for job in jobs)
                          else 'An actor start needs review in Apify before retrying.'
                          if any(job['state'] in {'starting', 'needs_review'} and not job.get('run_id') for job in jobs)
                          else None),
    }


def _new_generation(state, as_of, max_results, include_details, root):
    previous = state.get('generation')
    if previous:
        # Retain the complete prior checkpoint and its cached county files. The
        # lifetime reservation ledger remains in the active state unchanged.
        archive = _paths(root)[0] / 'apify-generations' / (previous['id'] + '.json')
        atomic_json(archive, previous, True)
        _sync_parents(archive, root)
        state['history'] = [*state['history'], {
            'id': previous['id'], 'started_at': previous['started_at'],
            'max_results': previous['max_results'],
            'include_details': previous.get('include_details', False),
            'completed': sum(job['state'] == 'succeeded' for job in previous['jobs']),
        }][-20:]
    generation = {'id': uuid.uuid4().hex, 'started_at': now(), 'as_of': as_of.isoformat(),
                  'max_results': max_results, 'include_details': include_details,
                  'jobs': [{'county': county, 'state': 'pending', 'http_calls': 0, 'read_bytes': 0}
                           for county in BAY_COUNTIES]}
    state['generation'] = generation
    return generation


def _stage_path(root, generation, index):
    return _paths(root)[0] / 'apify-cache' / generation['id'] / (str(index) + '.json')


def _read_dataset(path):
    if not path.exists():
        return {'sales': []}
    try:
        previous = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(previous, dict) or not isinstance(previous.get('sales', previous.get('rows', [])), list):
            raise ValueError()
        return previous
    except (OSError, ValueError, TypeError):
        raise ApifyRefreshError('The saved dataset could not be read. Refresh stopped without changing it.') from None


def _attached_evidence(value):
    """Accept only explicit Zillow attachment evidence for one exact property ID."""
    if not isinstance(value, dict) or value.get('reason') != 'explicit_attached_home':
        return None
    try:
        identity = source_identity(value)
        if not re.fullmatch(r'zillow:[0-9]{1,30}', identity):
            return None
        source_id = identity.split(':', 1)[1]
        url = value.get('source_url')
        if not isinstance(url, str):
            return None
        parsed = urlparse(url)
        matched = re.search(r'/([0-9]+)_zpid/?$', parsed.path)
        if (parsed.scheme != 'https' or parsed.hostname not in {'zillow.com', 'www.zillow.com'}
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or not parsed.path.startswith('/homedetails/') or not matched
                or matched.group(1) != source_id or '\\' in url
                or any(ord(c) < 32 for c in url)):
            return None
        lat, lng = finite_number(value.get('lat')), finite_number(value.get('lng'))
        if not in_bay_area(lat, lng):
            return None
        sold = parse_date(value.get('sold_date')).isoformat()
        evidence, address = value.get('attachment_evidence'), value.get('address')
        if (not isinstance(evidence, str) or not isinstance(address, str) or not address.strip()
                or not re.fullmatch(r'Zillow (record|resoFacts|facts)(\.(isAttached|is_attached|hasAttachedProperty) true'
                                    r'|\.(isDetached|is_detached) false'
                                    r'| property subtype explicitly indicates an attached home)', evidence)):
            return None
        clean = {key: str(value.get(key, ''))[:1000] for key in
                 ('address', 'street_address', 'city', 'attachment_evidence')}
        return dict(clean, id=source_id, source_id=source_id, source_url=url,
                    lat=lat, lng=lng, sold_date=sold, reason='explicit_attached_home')
    except (ValueError, TypeError):
        return None


def _publish(manager, state, as_of):
    generation = state['generation']
    path = manager.data / 'sales.json'
    previous = _read_dataset(path)
    completed_runs = sorted(job['run_id'] for job in generation['jobs'] if job['state'] == 'succeeded')
    def checked(result):
        # Record no-op/empty publication too, so recovery does not offer a
        # permanently pending local publication when no rows were eligible.
        generation['publication_checked_run_ids'] = completed_runs
        _save(manager.root, state)
        return result
    last = previous.get('last_api_refresh') or {}
    ids = last.get('published_run_ids', []) if isinstance(last, dict) else []
    published = set(ids) if isinstance(ids, list) and all(isinstance(value, str) for value in ids) else set()
    if (isinstance(last, dict) and last.get('generation') == generation['id']
            and set(completed_runs).issubset(published)):
        return checked(None)
    rows, regions = [], {}
    exclusions = {}
    for raw in previous.get('excluded_attached_properties', []):
        evidence = _attached_evidence(raw)
        if evidence:
            exclusions[source_identity(evidence)] = evidence
    previous_exclusion_ids = set(exclusions)
    for index, job in enumerate(generation['jobs']):
        regions[job['county']] = {key: job.get(key) for key in
                                 ('state', 'input_count', 'eligible_count', 'result_limit_reached',
                                  'provider_cap_reached', 'dataset_complete')}
        if job['state'] != 'succeeded':
            continue
        try:
            staged = json.loads(_stage_path(manager.root, generation, index).read_text(encoding='utf-8'))
            if not isinstance(staged, dict) or not isinstance(staged.get('sales'), list):
                raise ValueError()
            rows.extend(staged['sales'])
            diagnostics = staged.get('diagnostics') or {}
            evidence_rows = diagnostics.get('explicit_attached_exclusions', []) if isinstance(diagnostics, dict) else []
            if isinstance(evidence_rows, list):
                for raw in evidence_rows:
                    evidence = _attached_evidence(raw)
                    if evidence:
                        exclusions[source_identity(evidence)] = evidence
        except (OSError, ValueError, TypeError):
            raise ApifyRefreshError('A completed county checkpoint could not be read. The saved map is intact.') from None
    baseline = previous.get('sales', previous.get('rows', []))
    retained = [row for row in baseline if not isinstance(row, dict) or source_identity(row) not in exclusions]
    incoming = [row for row in rows if not isinstance(row, dict) or source_identity(row) not in exclusions]
    removed = len(baseline) - len(retained)
    merged, counts = merge_sales(retained, incoming, as_of=as_of,
                                 market_sale_quarantine=previous.get('market_sale_quarantine'))
    counts.update(removed_attached=removed, excluded_incoming_attached=len(rows) - len(incoming),
                  attached_exclusions_added=len(set(exclusions) - previous_exclusion_ids),
                  attached_exclusions_total=len(exclusions))
    if (not counts['incoming_eligible'] and not removed and not counts['attached_exclusions_added']
            and not counts['removed_quarantined']):
        return checked(None)
    sources = sorted({source_provider(row['source_url']) for row in merged} - {None})
    completed = sum(job['state'] == 'succeeded' for job in generation['jobs'])
    coverage = dict(previous.get('coverage', {}))
    coverage.update(status='partial', complete=False, scope='bay_area', refresh_mode='automatic_api',
                    label='Bay Area county searches', regions=regions, counties_completed=completed,
                    counties_total=len(BAY_COUNTIES), county_searches_finished=completed == len(BAY_COUNTIES),
                    counts=dict(sorted(Counter(row['city'] for row in merged).items())),
                    source_counts=dict(sorted(Counter(row['source'] for row in merged).items())),
                    merge_counts=counts,
                    explicit_attached_exclusions=len(exclusions),
                    note='Searches cover all nine Bay Area counties, including unincorporated areas. '
                         'Progress shows finished searches, not proof of every sale. Zillow search limits, '
                         'actor cost/result caps and missing fields can omit homes; retained snapshots keep their original sale dates.')
    budget = status(manager.root)
    if budget.get('paid_allowance'):
        budget['paid_allowance']['publication_pending'] = False
    snapshot = dict(previous, updated_at=now(), source=' + '.join(sources) + ' sold-home snapshots',
                    sources=sources, sales=merged, coverage=coverage,
                    excluded_attached_properties=[exclusions[key] for key in sorted(exclusions)],
                    last_api_refresh={'provider': PROVIDER, 'generation': generation['id'],
                                      'include_details': generation.get('include_details', False),
                                      'counts': counts, 'published_run_ids': completed_runs,
                                      'budget': budget})
    snapshot.pop('rows', None)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    archive = manager.data / 'snapshots' / ('sales-' + stamp + '.json')
    atomic_json(archive, snapshot)
    _sync_parents(archive, manager.root)
    atomic_json(path, snapshot)
    return checked(snapshot)


def run_refresh(manager, cfg, *, client_factory=None, normalize=None, input_builder=None, as_of=None):
    """Run or resume county jobs. Caller holds the manager's operation lock.

    Inject the client/parser in tests. Production uses apify_source.ApifyClient.
    Each button press starts at most apify_max_runs_per_refresh new runs; known
    active runs resume without reserving or starting a duplicate actor run.
    """
    explicit_as_of = as_of
    as_of = as_of or today_utc()
    try:
        paid = _paid_allowance(cfg, check_expiry=False)
        if paid is None and cfg.get('apify_free_plan_verified') is not True:
            raise ApifyRefreshError('Verify the Apify free plan and available credits before enabling refresh. No request was made.')
        if not cfg.get('api_key'):
            raise ApifyRefreshError('Connect an Apify API token before refreshing. No request was made.')
        limit = paid['usage_budget_micro_usd'] if paid else _money(cfg.get('apify_credit_budget_usd', 0.10), 'credit allowance')
        run_cap = _money(cfg.get('apify_max_cost_per_run_usd', 0.08), 'actor run cap')
        buffer = _money(cfg.get('apify_api_buffer_usd', 0.02), 'API allowance')
        if buffer < (250_000 if paid else 20_000):
            raise ApifyRefreshError('The API/storage allowance must be at least ' + ('$0.25' if paid else '$0.02') + ' per county run.')
        county_caps = cfg.get('apify_county_run_caps_usd')
        if county_caps is not None:
            if not isinstance(county_caps, dict) or set(county_caps) != set(BAY_COUNTIES):
                raise ApifyRefreshError('County actor caps must explicitly cover all nine Bay Area counties.')
            county_caps = {county: _money(value, 'county actor cap') for county, value in county_caps.items()}
        if run_cap > 100_000_000 or any(value > 100_000_000 for value in (county_caps or {}).values()):
            raise ApifyRefreshError('An actor run cap exceeds the supported $100 limit.')
        max_runs = _integer(cfg.get('apify_max_runs_per_refresh', 1), 'run count', 1, 9)
        max_results = _integer(cfg.get('apify_max_items_per_county', 100000), 'result limit', 1, 100000)
        run_timeout = _integer(cfg.get('apify_run_timeout_seconds', 600), 'actor timeout', 1, MAX_RUN_TIMEOUT_SECONDS)
        poll_interval = _integer(cfg.get('apify_poll_interval_seconds', 5), 'poll interval', 1, MAX_POLL_INTERVAL_SECONDS)
        include_details = cfg.get('apify_include_details', True)
        if not isinstance(include_details, bool):
            raise ApifyRefreshError('Detailed sale data must be enabled or disabled with a boolean value.')
        _read_dataset(manager.data / 'sales.json')
        state = _load(manager.root)
        saved_paid = state.get('paid_allowance')
        if saved_paid and (paid is None or paid != {key: value for key, value in saved_paid.items() if key != 'generation_id'}):
            raise ApifyRefreshError('The saved one-time allowance cannot be removed, replaced, increased or extended automatically. No request was made.')
        generation = state.get('generation')
        criteria_changed = bool(generation and (generation['max_results'] != max_results
            or generation.get('include_details', False) != include_details))
        has_active = bool(generation and any(job['state'] in {'starting', 'running', 'needs_review'}
                                             for job in generation['jobs']))
        if paid and not saved_paid and has_active:
            raise ApifyRefreshError('An existing actor run needs to finish or be reviewed before starting the one-time paid import.')
        if saved_paid and (not generation or saved_paid.get('generation_id') != generation['id']):
            raise ApifyRefreshError('The one-time allowance belongs to a different import generation. No request was made.')
        if saved_paid and all(job['state'] in {'succeeded', 'failed'} for job in generation['jobs']):
            # A crash can occur after the final county checkpoint but before
            # publication. Recover locally, even after expiry, without buying
            # another run or extending the authorization.
            snapshot = _publish(manager, state, explicit_as_of or today_utc())
            manager.update(state='success' if snapshot else 'partial', finished_at=now(),
                           apify_budget=status(manager.root),
                           message='The one-time import is finished. '
                               + ('Saved results were published. ' if snapshot else 'No new eligible results were published. ')
                               + 'No additional actor run was purchased.')
            return {'state': manager.current['state'], 'published': snapshot is not None, 'coverage_complete': False,
                    'allowance_consumed': True, 'counties_completed': sum(job['state'] == 'succeeded' for job in generation['jobs'])}
        try:
            _check_paid_expiry(paid)
        except ApifyRefreshError:
            if not saved_paid or not any(job['state'] == 'succeeded' for job in generation['jobs']):
                raise
            # The matching grant, generation and checkpoints were validated
            # above. Publishing saved successful counties requires no API
            # calls, new reservations, or changes to unfinished jobs.
            snapshot = _publish(manager, state, explicit_as_of or today_utc())
            completed = sum(job['state'] == 'succeeded' for job in generation['jobs'])
            manager.update(state='partial', finished_at=now(), apify_budget=status(manager.root),
                           progress={'completed': completed, 'total': len(BAY_COUNTIES)},
                           message='The one-time allowance has expired. '
                               + ('Saved county results were published offline. ' if snapshot else 'Saved results are already published or contain no new eligible sales. ')
                               + 'Unfinished county searches remain incomplete; no API request was made.')
            return {'state': 'partial', 'published': snapshot is not None, 'coverage_complete': False,
                    'allowance_expired': True, 'counties_completed': completed}
        if saved_paid and criteria_changed:
            raise ApifyRefreshError('The one-time import cannot change its saved criteria. Restore its original settings to resume; no additional actor run was purchased.')
        criteria_pending = criteria_changed and has_active
        if (generation is None or (paid and not saved_paid) or (criteria_changed and not has_active)
                or all(job['state'] in {'succeeded', 'failed'} for job in generation['jobs'])):
            generation = _new_generation(state, as_of, max_results, include_details, manager.root)
            _save(manager.root, state)
        if client_factory is None or normalize is None or input_builder is None:
            from apify_source import ApifyClient, normalize_records, build_input
            client_factory = client_factory or ApifyClient
            normalize = normalize or normalize_records
            input_builder = input_builder or build_input
        started, budget_hit, fresh_eligible = 0, False, False
        for index, job in enumerate(generation['jobs']):
            if job['state'] in {'succeeded', 'failed'}:
                continue
            if criteria_pending and job['state'] == 'pending':
                # Finish a known active run, but do not buy additional runs with
                # obsolete criteria or discard an uncertain start checkpoint.
                continue
            if job['state'] in {'starting', 'needs_review'} and not job.get('run_id'):
                job['state'] = 'needs_review'
                _save(manager.root, state)
                raise ApifyRefreshError('An earlier actor start has an uncertain outcome. Check the Apify run history before retrying; no duplicate run was started.')
            if job['state'] == 'pending':
                if started >= max_runs:
                    break
                _check_paid_expiry(paid)
                job_cap = (county_caps or {}).get(job['county'], run_cap)
                reserved = sum(item['micro_usd'] for item in state['reservations']
                               if not paid or item.get('paid_allowance_id') == paid['id'])
                if reserved + job_cap + buffer > limit:
                    budget_hit = True
                    break
                bounded_timeout = _bounded_timeout(run_timeout, paid)
                reservation = {'id': uuid.uuid4().hex, 'micro_usd': job_cap + buffer,
                               'actor_micro_usd': job_cap, 'api_micro_usd': buffer,
                               'at': now(), 'generation': generation['id'], 'county': job['county']}
                if paid:
                    reservation['paid_allowance_id'] = paid['id']
                    state['paid_allowance'] = dict(paid, generation_id=generation['id'])
                state['reservations'].append(reservation)
                job.update(state='starting', reservation_id=reservation['id'],
                           actor_micro_usd=job_cap, started_at=now(), run_timeout_seconds=bounded_timeout,
                           poll_interval_seconds=poll_interval)
                _save(manager.root, state)  # Durable reservation before an external side effect.
                started += 1
            elif poll_interval > job.get('poll_interval_seconds', 5):
                # Operational pacing may slow on resume; the remote run's
                # original timeout, its reservation, and all counters stay put.
                job['poll_interval_seconds'] = poll_interval
                _save(manager.root, state)

            def before_request(_method, _path):
                _check_paid_expiry(paid)
                if job['http_calls'] >= MAX_HTTP_CALLS:
                    raise ApifyRefreshError('The county API request limit was reached. Its saved run can be inspected in Apify; no duplicate run was started.')
                job['http_calls'] += 1
                _save(manager.root, state)

            def reserve_response(size):
                _integer(size, 'response reservation', 1, MAX_RESPONSE_BYTES)
                if job['read_bytes'] + size > MAX_RESPONSE_BYTES:
                    raise ApifyRefreshError('The county download allowance is exhausted. No request was made.')
                job['read_bytes'] += size
                _save(manager.root, state)

            def settle_response(reserved_size, actual_size):
                _integer(actual_size, 'response size', 0, reserved_size)
                if job['read_bytes'] < reserved_size:
                    raise ApifyRefreshError('The download reservation could not be verified.')
                job['read_bytes'] -= reserved_size - actual_size
                _save(manager.root, state)

            manager.update(message='Refreshing ' + job['county'] + ' from Zillow…',
                           progress={'completed': sum(x['state'] == 'succeeded' for x in generation['jobs']), 'total': 9})
            old_bytes = job['read_bytes']
            if MAX_RESPONSE_BYTES - old_bytes < 1024:
                raise ApifyRefreshError('The county response-size allowance was reached. The saved map is intact.')
            if job['http_calls'] >= MAX_HTTP_CALLS:
                raise ApifyRefreshError('The county API request limit was reached. No duplicate run was started.')
            client = client_factory(cfg['api_key'], max_total_charge_usd=job['actor_micro_usd'] / 1_000_000,
                                    max_http_calls=MAX_HTTP_CALLS - job['http_calls'],
                                    max_dataset_items=generation['max_results'],
                                    max_total_response_bytes=MAX_RESPONSE_BYTES - old_bytes,
                                    wait_timeout=_bounded_timeout(max(job.get('run_timeout_seconds', 600), run_timeout), paid),
                                    poll_interval=job.get('poll_interval_seconds', 5),
                                    before_request=before_request, reserve_response=reserve_response,
                                    settle_response=settle_response)
            try:
                if not job.get('run_id'):
                    run = client.start(input_builder([job['county']], max_results=generation['max_results'],
                                                     include_details=generation.get('include_details', False)))
                    run_id = run.get('id') if isinstance(run, dict) else None
                    if not isinstance(run_id, str) or not run_id.isalnum():
                        raise ApifyRefreshError('The actor start response did not contain a valid run ID. Check Apify before retrying.')
                    job.update(run_id=run_id, state='running')
                    _save(manager.root, state)
                run = _wait_with_recovery(client, job, state, manager.root, paid, run_timeout)
                dataset = client.dataset(run, max_results=generation['max_results'])
                if not isinstance(dataset, dict) or not isinstance(dataset.get('items'), list):
                    raise ApifyRefreshError('The actor dataset format changed. The saved map is intact.')
                parsed = normalize(dataset['items'], as_of=as_of)
                if not isinstance(parsed, dict) or not isinstance(parsed.get('sales'), list):
                    raise ApifyRefreshError('The sold-home parser returned invalid data. The saved map is intact.')
                staged = {'sales': parsed['sales'], 'diagnostics': parsed.get('diagnostics', {}),
                          'county': job['county'], 'run_id': job['run_id'], 'retrieved_at': now()}
                stage_path = _stage_path(manager.root, generation, index)
                atomic_json(stage_path, staged, True)
                _sync_parents(stage_path, manager.root)
                count = len(dataset['items'])
                from model import prepare_sales
                eligible, _ = prepare_sales(parsed['sales'], as_of=as_of)
                fresh_eligible = fresh_eligible or bool(eligible)
                job.update(state='succeeded', finished_at=now(), input_count=count,
                           eligible_count=len(eligible),
                           result_limit_reached=count >= generation['max_results'],
                           provider_cap_reached=bool(dataset.get('cap_reached', False)),
                           dataset_complete=dataset.get('dataset_complete') is True
                               and not dataset.get('cap_reached', False)
                               and count < generation['max_results'])
                _save(manager.root, state)
            except Exception as error:
                known_id = getattr(error, 'run_id', None)
                if not job.get('run_id') and isinstance(known_id, str) and known_id.isalnum():
                    job.update(run_id=known_id, state='running')
                if not job.get('run_id'):
                    job['state'] = 'needs_review'
                elif getattr(error, 'code', '') == 'unconfirmed_timeout':
                    job.update(state='needs_review', requires_run_confirmation=True)
                elif (job.get('timeout_recovery') or {}).get('phase') == 'intent':
                    job['state'] = 'needs_review'
                elif getattr(error, 'code', '') in {'run_failed', 'run_aborted', 'run_timed_out', 'terminal'}:
                    job['state'] = 'failed'
                _save(manager.root, state)
                if isinstance(error, ApifyRefreshError):
                    raise
                raise ApifyRefreshError('The Apify request did not finish. Its checkpoint and credit reservation were kept; the saved map is intact.') from None
        snapshot = _publish(manager, state, explicit_as_of or today_utc())
        completed = sum(job['state'] == 'succeeded' for job in generation['jobs'])
        message = f'{completed}/9 county searches finished. '
        if budget_hit:
            message += 'The saved ' + ('one-time paid' if paid else 'free-credit') + ' allowance is exhausted. '
        if criteria_pending:
            message += 'Saved search settings will apply on the next refresh after active runs finish. '
        message += 'Coverage remains partial; result and credit limits can omit homes.'
        manager.update(state='success' if snapshot else 'partial', message=message, finished_at=now(),
                       progress={'completed': completed, 'total': 9}, apify_budget=status(manager.root),
                       **({'live_verified': True, 'last_success': snapshot['updated_at']}
                          if snapshot and fresh_eligible and snapshot['last_api_refresh']['counts']['incoming_eligible'] else {}))
        return {'state': manager.current['state'], 'counties_completed': completed, 'budget_reached': budget_hit,
                'criteria_pending': criteria_pending, 'published': snapshot is not None, 'coverage_complete': False}
    except ApifyRefreshError as error:
        manager.update(state='error', message=str(error), finished_at=now())
        return {'state': 'error', 'published': False, 'coverage_complete': False}
