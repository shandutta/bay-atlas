"""On-demand, bounded Zillow-data imports through HouseData on RapidAPI.

No network call runs until an API key is configured and Refresh is pressed.
The provider adapter follows https://housedata.dev/docs/ and /listings/.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import threading
import tempfile
import time
from datetime import datetime, timezone, date
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from model import merge_sales, prepare_sales, source_provider, two_year_cutoff, today_utc

HOST = 'zillow-full-data-api.p.rapidapi.com'
PRICING_URL = 'https://rapidapi.com/bachhungcong/api/zillow-full-data-api/pricing'
PROVIDER = 'Zillow via HouseData / RapidAPI'
EAST_BAY = ['Oakland, CA','Berkeley, CA','Alameda, CA','Lafayette, CA','Moraga, CA','Orinda, CA']
# Cities are round-robin paginated. Large searches are visibly marked capped.
BAY_AREA = EAST_BAY + [x+', CA' for x in [
    'Piedmont','Albany','El Cerrito','Richmond','San Pablo','Pinole','Hercules',
    'El Sobrante','Kensington','San Leandro','San Lorenzo','Hayward','Castro Valley',
    'Fremont','Newark','Union City','Dublin','Pleasanton','Livermore','Walnut Creek',
    'Concord','Pleasant Hill','Martinez','Danville','San Ramon','Alamo','Clayton',
    'Brentwood','Antioch','Pittsburg','Oakley','San Francisco','Daly City','Pacifica',
    'South San Francisco','San Bruno','Millbrae','Burlingame','Hillsborough','San Mateo',
    'Foster City','Belmont','San Carlos','Redwood City','Woodside','Portola Valley',
    'Menlo Park','Atherton','East Palo Alto','Half Moon Bay','Palo Alto','Mountain View',
    'Los Altos','Los Altos Hills','Sunnyvale','Santa Clara','Cupertino','San Jose',
    'Campbell','Los Gatos','Saratoga','Milpitas','Morgan Hill','Gilroy','Sausalito',
    'Mill Valley','Tiburon','Belvedere','Corte Madera','Larkspur','Kentfield','Ross',
    'San Anselmo','Fairfax','San Rafael','Novato','Petaluma','Rohnert Park','Cotati',
    'Santa Rosa','Sebastopol','Sonoma','Windsor','Healdsburg','Napa','Yountville',
    'Saint Helena','Calistoga','Vallejo','Benicia','Fairfield','Suisun City','Vacaville','Dixon']]

def now():
    return datetime.now(timezone.utc).isoformat()

def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default

def atomic_json(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temp = Path(temporary_name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600 if private else 0o644)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def number(value):
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) else 0
    except (ValueError, TypeError, OverflowError):
        return 0

def sale_date(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 100000000000:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            pass
    return None

def normalize_property(record, expected_id=None):
    """Use an explicit SOLD price-history event, never asking price/Zestimate."""
    if not isinstance(record, dict):
        return None
    p = record.get('property', record)
    if not isinstance(p, dict):
        return None
    zpid = str(p.get('zpid', ''))
    if not zpid.isdigit() or (expected_id is not None and zpid != str(expected_id)):
        return None
    facts, address, location = p.get('facts') or {}, p.get('address') or {}, p.get('location') or {}
    if not all(isinstance(value, dict) for value in (facts, address, location)):
        return None
    typ = re.sub('[^a-z]', '', str(facts.get('homeType', '')).lower())
    if typ not in {'singlefamily', 'singlefamilyresidence', 'singlefamilyhome'}:
        return None
    subtype = str(facts.get('propertySubType', facts.get('propertySubtype', ''))).lower()
    if any(t in subtype for t in ('townhouse', 'townhome', 'condo')) or re.search(r'\battached\b', subtype):
        return None
    history = p.get('priceHistory') or []
    if not isinstance(history, list):
        return None
    sales = []
    as_of = today_utc()
    for event in history:
        if not isinstance(event, dict) or str(event.get('event', '')).strip().lower() != 'sold':
            continue
        when = sale_date(event.get('date') or event.get('time'))
        price = number(event.get('price'))
        if when and price > 0 and two_year_cutoff(as_of).isoformat() <= when <= as_of.isoformat():
            sales.append((when, price))
    if not sales:
        return None
    sold, price = max(sales)
    street = address.get('street') or address.get('streetAddress') or ''
    city = address.get('city') or ''
    row = dict(id=zpid,address=', '.join(str(x) for x in [street,city,address.get('state'),address.get('zip') or address.get('zipcode')] if x),
        city=city, lat=location.get('lat'),lng=location.get('lng'),price=price,
        sqft=facts.get('livingArea'),beds=facts.get('bedrooms'),sold_date=sold,
        home_type='singleFamily',source_url='https://www.zillow.com/homedetails/'+zpid+'_zpid/',
        date_evidence='HouseData Zillow priceHistory: Sold event',
        price_evidence='Actual Sold event price; not list price or Zestimate',retrieved_at=now())
    if facts.get('isAttached') is True:
        row['is_attached'] = True
    valid, _ = prepare_sales([row], as_of=as_of)
    return row if valid else None

class BudgetReached(Exception):
    pass

class ProviderError(Exception):
    pass


class ImportBusyError(RuntimeError):
    pass


def dataset_sources(rows):
    return sorted({source_provider(row.get('source_url', '')) for row in rows} - {None})


def public_apify_allowance(cfg, budget, root=None):
    """Describe the local allowance without exposing authorization or billing data."""
    from apify_refresh import ApifyRefreshError, _load, _paid_allowance
    raw = cfg.get('apify_paid_allowance')
    ledger = (budget or {}).get('paid_allowance')
    if raw is None and ledger is None:
        return {'mode': 'free'}
    public = {'mode': 'paid', 'state': 'invalid', 'total_usd': None,
              'used_usd': None, 'remaining_usd': None, 'expires_at': None,
              'usage_total_usd': None, 'usage_reserved_usd': None,
              'usage_remaining_usd': None}
    if not isinstance(raw, dict):
        return public
    # A disabled but otherwise valid grant can still show its bounds. Never
    # alter the saved grant, and never use this display-only copy for execution.
    display_cfg = dict(cfg, apify_paid_allowance=dict(raw, enabled=True))
    try:
        grant = _paid_allowance(display_cfg, check_expiry=False)
    except ApifyRefreshError:
        public['state'] = 'disabled' if raw.get('enabled') is False else 'invalid'
        return public
    public.update(total_usd=grant['total_budget_micro_usd'] / 1_000_000,
                  usage_total_usd=grant['usage_budget_micro_usd'] / 1_000_000,
                  expires_at=grant['expires_at'])
    if budget is None:
        return public
    reserved = round(number((ledger or {}).get('reserved_usd', 0)) * 1_000_000)
    committed = (grant['fixed_cash_paid_micro_usd'] + grant['tax_nonusage_reserve_micro_usd']
                 + max(0, reserved - grant['included_usage_credit_micro_usd']))
    remaining_usage = max(0, grant['usage_budget_micro_usd'] - reserved)
    public.update(used_usd=committed / 1_000_000,
                  remaining_usd=max(0, grant['total_budget_micro_usd'] - committed) / 1_000_000,
                  usage_reserved_usd=reserved / 1_000_000,
                  usage_remaining_usd=remaining_usage / 1_000_000,
                  state='active')
    jobs = budget.get('jobs', [])
    pending = next((job for job in jobs if job.get('state') == 'pending'), None)
    county_caps = cfg.get('apify_county_run_caps_usd') or {}
    if not isinstance(county_caps, dict):
        public['state'] = 'invalid'
        return public
    next_cap = number(county_caps.get((pending or {}).get('county'), cfg.get('apify_max_cost_per_run_usd', .08)))
    next_cost = round((next_cap + number(cfg.get('apify_api_buffer_usd', .25))) * 1_000_000)
    if raw.get('enabled') is not True:
        public['state'] = 'disabled'
    elif (ledger or {}).get('publication_pending'):
        # The backend's recovery branch only publishes already downloaded
        # records. Expiry may pass, but the original valid grant must match.
        try:
            saved = (_load(root).get('paid_allowance') or {}) if root is not None else {}
            matches = grant == {key:value for key,value in saved.items() if key != 'generation_id'}
        except ApifyRefreshError:
            matches = False
        public['state'] = 'publication_pending' if matches else 'invalid'
    elif datetime.now(timezone.utc) >= datetime.fromisoformat(grant['expires_at']):
        public['state'] = 'expired'
    elif (ledger or {}).get('generation_finished'):
        public['state'] = 'completed'
    elif budget.get('paused_reason'):
        public['state'] = 'paused'
    elif remaining_usage < next_cost and not any(job.get('state') == 'running' for job in jobs):
        public['state'] = 'exhausted'
    return public


class RefreshManager:
    def __init__(self, root):
        self.root = Path(root)
        self.data = self.root / 'data'
        self.config_path = self.root / 'private' / 'provider.json'
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.worker = None
        self.current = read_json(self.data / 'refresh-status.json', {})
        if self.current.get('state') == 'running':
            self.current.update(state='error', message='The previous refresh was interrupted. The saved map is intact.')
        self.last_call = 0

    def config(self):
        cfg = dict({'provider_kind':'housedata','scope':'east_bay','max_calls_per_refresh':900,'monthly_call_limit':900,
                    'apify_max_cost_per_run_usd':0.08,'apify_credit_budget_usd':0.10,
                    'apify_max_runs_per_refresh':1,'apify_max_items_per_county':10000,
                    'apify_include_details':True},
                    **read_json(self.config_path, {}))
        if cfg['provider_kind'] == 'apify':
            cfg['api_key'] = cfg.get('apify_api_key', '')
        return cfg

    def usage(self):
        # A rolling 31-day window is deliberately conservative across billing dates.
        path = self.data / 'provider-usage.json'
        if not path.exists():
            return []
        try:
            usage = json.loads(path.read_text())
            if not isinstance(usage,list) or any(not isinstance(x,dict) or number(x.get('at')) <= 0 for x in usage):
                raise ValueError('Invalid usage ledger')
        except (OSError,ValueError):
            # Never reset billing protection to zero after a damaged local file.
            raise ProviderError('The local request usage log could not be read. Refresh is paused to protect the request budget.') from None
        return [x for x in usage if number(x.get('at')) > time.time()-31*86400]

    def provider_status(self):
        with self.lock:
            cfg = self.config()
            if cfg['provider_kind'] == 'apify':
                from apify_refresh import ApifyRefreshError, status
                try:
                    budget, usage_error = status(self.root), None
                except ApifyRefreshError as error:
                    budget, usage_error = None, str(error)
                allowance = public_apify_allowance(cfg, budget, self.root)
                return {key:cfg[key] for key in ('provider_kind','scope','apify_max_cost_per_run_usd',
                    'apify_credit_budget_usd','apify_max_runs_per_refresh','apify_max_items_per_county',
                    'apify_include_details')} | {
                    'configured':bool(cfg.get('api_key')), 'provider':'Zillow via Apify',
                    'pricing_url':'https://apify.com/topapi/zillow-scraper/pricing',
                    'live_verified':bool(self.current.get('live_verified') and self.current.get('provider_kind') == 'apify'),
                    'budget_window':('one import; no automatic renewal' if allowance['mode'] == 'paid'
                                     else 'lifetime credit allowance; no automatic reset'),
                    'budget':budget, 'usage_error':usage_error,
                    'allowance':allowance,
                    'apify_free_plan_verified':bool(cfg.get('apify_free_plan_verified')),
                    'message':usage_error or ('Uses a bounded one-time allowance installed on this server. It cannot renew or increase from this page.'
                        if allowance['mode'] == 'paid' else 'Uses the approved credit allowance. No subscription or payment method is added by this app.')}
            usage_error = None
            try:
                calls_used = len(self.usage())
            except ProviderError as error:
                calls_used, usage_error = None, str(error)
            return {k:cfg[k] for k in ('scope','max_calls_per_refresh','monthly_call_limit')} | {
                'configured':bool(cfg.get('api_key')),'provider':PROVIDER,'provider_kind':'housedata',
                'apify_allowance_mode':'paid' if cfg.get('apify_paid_allowance') is not None else 'free',
                'calls_used':calls_used,'pricing_url':PRICING_URL,
                'budget_window':'rolling 31 days','live_verified':bool(self.current.get('live_verified')),
                'usage_error':usage_error,
                'message':usage_error or 'API request caps apply only to this app; other uses of the same key share the provider quota.'}

    def configure(self, payload):
        with self.lock:
            if self.current.get('state') == 'running':
                raise ValueError('Wait for the current refresh to finish before changing the connection.')
            cfg = read_json(self.config_path, {})
            allowed = {'provider_kind', 'api_key', 'scope', 'max_calls_per_refresh', 'monthly_call_limit',
                       'apify_free_plan_verified', 'apify_include_details', 'apify_max_cost_per_run_usd',
                       'apify_credit_budget_usd', 'apify_max_runs_per_refresh', 'apify_max_items_per_county'}
            if not isinstance(payload, dict) or set(payload) - allowed:
                raise ValueError('This connection setting cannot be changed from the browser.')
            if cfg.get('apify_paid_allowance') is not None and any(key.startswith('apify_') for key in payload):
                raise ValueError('The one-time allowance and its refresh limits are managed by the server operator.')
            old_kind = cfg.get('provider_kind', 'housedata')
            key_changed = False
            replacing_apify_key = False
            kind = payload.get('provider_kind', cfg.get('provider_kind', 'housedata'))
            if kind not in ('housedata','apify'):
                raise ValueError('Unknown data provider.')
            cfg['provider_kind'] = kind
            if payload.get('api_key'):
                key = str(payload['api_key']).strip()
                if len(key) < 16 or len(key) > 256 or not re.fullmatch(r'[A-Za-z0-9_.-]+',key):
                    raise ValueError('Enter a valid provider API key.')
                key_field = 'apify_api_key' if kind == 'apify' else 'api_key'
                key_changed = cfg.get(key_field) != key
                replacing_apify_key = kind == 'apify' and key_changed and bool(cfg.get(key_field))
                cfg[key_field] = key
                if kind == 'apify' and key_changed:
                    cfg['apify_free_plan_verified'] = False
                    if isinstance(cfg.get('apify_paid_allowance'), dict):
                        cfg['apify_paid_allowance']['enabled'] = False
            if 'apify_free_plan_verified' in payload:
                if not isinstance(payload['apify_free_plan_verified'], bool):
                    raise ValueError('Free plan confirmation must be true or false.')
                cfg['apify_free_plan_verified'] = payload['apify_free_plan_verified'] if not replacing_apify_key else False
            if 'apify_include_details' in payload:
                if not isinstance(payload['apify_include_details'], bool):
                    raise ValueError('Detailed sale data must be true or false.')
                cfg['apify_include_details'] = payload['apify_include_details']
            for key in ('apify_max_cost_per_run_usd','apify_credit_budget_usd'):
                if key in payload:
                    value = number(payload[key])
                    if isinstance(payload[key], bool) or not 0.01 <= value <= 100 or round(value,2) != value:
                        raise ValueError('Credit limits must be between $0.01 and $100, in whole cents.')
                    cfg[key] = value
            for key, upper in [('apify_max_runs_per_refresh',9),('apify_max_items_per_county',100000)]:
                if key in payload:
                    value = payload[key]
                    if isinstance(value,bool) or not str(value).isdigit() or not 1 <= int(value) <= upper:
                        raise ValueError(key+' is outside the supported range.')
                    cfg[key] = int(value)
            for key, upper in [('max_calls_per_refresh',10000),('monthly_call_limit',100000)]:
                if key in payload:
                    v = payload[key]
                    if isinstance(v, bool) or not str(v).isdigit() or not 1 <= int(v) <= upper:
                        raise ValueError(key+' is outside the supported range.')
                    cfg[key] = int(v)
            if 'scope' in payload:
                if payload['scope'] not in ('east_bay','bay_area'):
                    raise ValueError('Unknown coverage scope.')
                cfg['scope'] = payload['scope']
            if kind == 'apify':
                cfg['scope'] = 'bay_area'
            self.config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.config_path.parent,0o700)
            atomic_json(self.config_path,cfg,True)
            if kind != old_kind or key_changed:
                self.update(state='idle', live_verified=False, provider_kind=kind,
                            message='Connection changed. Refresh sales to verify the selected provider.')
            return self.provider_status()

    def status(self):
        with self.lock:
            cfg = self.provider_status()
            result = dict(self.current)
            result.setdefault('state','idle' if cfg['configured'] else 'unconfigured')
            result.setdefault('message','Ready to fetch fresh sales.' if cfg['configured'] else 'Connect a data provider to fetch fresh Zillow sales. The current map is a verified partial snapshot.')
            result.setdefault('progress', {'completed':0,'total':0})
            result.setdefault('last_success',None)
            return dict(result, configured=cfg['configured'],provider=cfg['provider'],
                        provider_kind=cfg['provider_kind'],calls_used=cfg.get('calls_used'))

    def update(self, **kwargs):
        with self.lock:
            self.current.update(kwargs)
            atomic_json(self.data / 'refresh-status.json',self.current)

    def start(self):
        with self.lock:
            if self.current.get('state') == 'running':
                return self.status()
            if self.operation_lock.locked():
                return dict(self.status(), state='busy', message='A data import is running. Try refresh after it finishes.')
            cfg = self.config()
            if not cfg.get('api_key'):
                self.update(state='unconfigured',message='Connect your data provider in Data connection, then refresh. No paid request has been made.')
                return self.status()
            try:
                calls_used = len(self.usage()) if cfg['provider_kind'] != 'apify' else 0
            except ProviderError as error:
                self.update(state='error',message=str(error))
                return self.status()
            if cfg['provider_kind'] != 'apify' and calls_used >= cfg['monthly_call_limit']:
                self.update(state='error',message='The local 31-day request limit has been reached. The saved map is intact.')
                return self.status()
            if not self.operation_lock.acquire(blocking=False):
                return dict(self.status(), state='busy', message='A data import is running. Try refresh after it finishes.')
            try:
                publishing = cfg['provider_kind'] == 'apify' and self.provider_status().get('allowance', {}).get('state') == 'publication_pending'
                self.update(state='running',provider_kind=cfg['provider_kind'],started_at=now(),
                            message='Publishing downloaded sales…' if publishing else 'Fetching Zillow sold listings…',
                            progress={'completed':0,'total':0 if publishing else cfg['max_calls_per_refresh']})
                self.worker = threading.Thread(target=self.run,args=(cfg, True),daemon=True,name='zillow-refresh')
                self.worker.start()
            except Exception:
                self.operation_lock.release()
                raise
            return self.status()

    def import_redfin(self, payload, *, data_path=None):
        """Import one user-supplied export; never obtains cookies or calls Redfin."""
        from redfin_csv import parse_csv
        csv_text, filename = payload.get('csv'), payload.get('filename', '')
        if not isinstance(csv_text, str) or not isinstance(filename, str):
            raise ValueError('Expected csv text and a filename.')
        if not csv_text.strip():
            raise ValueError('Choose a nonempty Redfin CSV export.')
        if len(csv_text.encode('utf-8')) > 5 * 1024 * 1024:
            raise ValueError('The Redfin CSV exceeds the 5 MiB input limit.')
        if not self.operation_lock.acquire(blocking=False):
            raise ImportBusyError('A refresh or import is running. Try again after it finishes.')
        try:
            imported_at = now()
            parsed = parse_csv(csv_text, as_of=today_utc(), export_filename=Path(filename).name[:255], imported_at=imported_at)
            if not parsed['sales']:
                raise ValueError('The CSV contains no eligible dated, sold single-family homes. The saved map is intact.')
            path = Path(data_path) if data_path is not None else self.data / 'sales.json'
            if path.exists():
                try:
                    previous = json.loads(path.read_text(encoding='utf-8'))
                    existing = previous.get('sales', previous.get('rows', []))
                    if not isinstance(previous, dict) or not isinstance(existing, list):
                        raise ValueError('Unexpected data structure')
                except (OSError, ValueError, AttributeError, TypeError):
                    raise RuntimeError('The saved dataset could not be read. Import stopped without changing it.') from None
            else:
                previous, existing = {}, []
            merged, merge = merge_sales(existing, parsed['sales'], as_of=today_utc())
            if merge['incoming_eligible'] == 0 or merge['conflicts'] == merge['incoming_eligible']:
                raise ValueError('No incoming sales could be safely accepted. Existing records were preserved; check the export for conflicting values.')
            counts = dict(input=parsed['diagnostics']['input_count'], eligible=merge['incoming_eligible'],
                          **{key: merge[key] for key in ('added', 'updated', 'duplicates', 'stale', 'conflicts', 'total')})
            audit = {key: parsed[key] for key in ('source', 'imported_at', 'export_sha256', 'export_filename',
                     'byte_count', 'observed_export_date_range', 'diagnostics')}
            audit.update(counts=counts, refresh_mode='manual_csv', coverage_complete=False,
                         search_filters='Not encoded in the CSV; eligibility is rechecked for every row.')
            conflict_warning = 'Some imported records conflicted with saved values. Existing values were preserved; see import history.'
            previous_warnings = previous.get('warnings', [])
            had_conflicts = any(item == conflict_warning or re.fullmatch(r'Kept existing values for \d+ conflicting sale records\.', item)
                                for item in previous_warnings if isinstance(item, str))
            warnings = list(dict.fromkeys(item for item in [*previous_warnings, *parsed['warnings']]
                if isinstance(item, str)
                and item != 'Partial starter snapshot. Live API refresh needs a RapidAPI connection.'
                and item != conflict_warning
                and not re.fullmatch(r'Kept existing values for \d+ conflicting sale records\.', item)))
            if counts['conflicts'] or had_conflicts:
                warnings.append(conflict_warning)
            sources = dataset_sources(merged)
            coverage = dict(previous.get('coverage', {}))
            coverage.update(status='partial', complete=False, refresh_mode='manual_csv', label='Imported sale snapshots',
                            counts=dict(sorted(Counter(row['city'] for row in merged).items())),
                            source_counts=dict(sorted(Counter(row['source'] for row in merged).items())),
                            note='Partial combined sale coverage. Redfin records came from a manually downloaded CSV; automatic Redfin refresh has not been connected. Local MLS export rules and search limits can omit homes.')
            snapshot = dict(previous, updated_at=imported_at, source=' + '.join(sources) + ' sold-home snapshots',
                            sources=sources, sales=merged, coverage=coverage, warnings=warnings,
                            last_import=audit, import_history=[*previous.get('import_history', []), audit][-20:])
            snapshot.pop('rows', None)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            atomic_json(path.parent / 'snapshots' / ('sales-' + stamp + '.json'), snapshot)
            atomic_json(path, snapshot)
            return dict(status='success', source='Redfin', refresh_mode='manual_csv', imported_at=imported_at,
                        export_sha256=parsed['export_sha256'], counts=counts, warnings=parsed['warnings'] +
                        ([f"Kept existing values for {counts['conflicts']} conflicting sale records."] if counts['conflicts'] else []),
                        coverage_complete=False)
        except OSError:
            raise RuntimeError('The import could not be saved. The prior dataset remains available.') from None
        finally:
            self.operation_lock.release()

    def request(self, cfg, path, body=None, params=None):
        with self.lock:
            if not cfg.get('api_key'):
                raise ProviderError('An API key must be configured before requesting data.')
            usage = self.usage()
            if self.run_calls >= cfg['max_calls_per_refresh'] or len(usage) >= cfg['monthly_call_limit']:
                raise BudgetReached()
            # Reserve before dispatch; failures count locally too. Never automatic retries.
            usage.append({'at':time.time()})
            atomic_json(self.data / 'provider-usage.json',usage,True)
            self.run_calls += 1
        time.sleep(max(0,0.55-(time.monotonic()-self.last_call)))
        self.last_call = time.monotonic()
        url = 'https://'+HOST+path+('?' + urlencode(params) if params else '')
        request = Request(url,data=json.dumps(body).encode() if body is not None else None,
            headers={'X-RapidAPI-Key':cfg['api_key'],'X-RapidAPI-Host':HOST,'Content-Type':'application/json','User-Agent':'BayAtlas/1.0'})
        try:
            with urlopen(request,timeout=35) as response:
                raw = response.read(5_000_001)
                if len(raw) > 5_000_000:
                    raise ProviderError('The provider response exceeded the local size limit.')
                result = json.loads(raw)
        except HTTPError as error:
            code = error.code
            if code in (401,403):
                raise ProviderError('The API key was refused. Check the HouseData subscription and key in Data connection.') from None
            if code == 429:
                raise ProviderError('The provider request quota or rate limit was reached. No automatic retry; the saved map is intact.') from None
            raise ProviderError('The provider returned HTTP '+str(code)+'. The saved map is intact.') from None
        except (URLError,TimeoutError,ValueError,OSError):
            raise ProviderError('The provider could not be reached or returned invalid data. The saved map is intact.') from None
        if not isinstance(result,dict) or result.get('error'):
            raise ProviderError('The provider returned an unsuccessful response. The saved map is intact.')
        self.update(progress={'completed':self.run_calls,'total':cfg['max_calls_per_refresh']})
        return result

    def run(self,cfg, reserved=False):
        if not reserved:
            if not self.operation_lock.acquire(blocking=False):
                raise ImportBusyError('A refresh or import is already running.')
        try:
            return self._run(cfg)
        except Exception:
            self.update(state='error', message='Refresh stopped before completion. Inspect the saved dataset before trying again.', finished_at=now())
        finally:
            self.operation_lock.release()

    def _run(self,cfg):
        if cfg.get('provider_kind') == 'apify':
            from apify_refresh import run_refresh
            return run_refresh(self, cfg)
        self.run_calls = 0
        path = self.data / 'sales.json'
        try:
            previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'sales': []}
            if not isinstance(previous, dict) or not isinstance(previous.get('sales', previous.get('rows', [])), list):
                raise ValueError('Invalid dataset')
        except (OSError, ValueError, TypeError):
            self.update(state='error', message='The saved dataset could not be read. Refresh stopped without changing it.', finished_at=now())
            return
        rows, seen, region_counts = [], set(), {}
        regions = EAST_BAY if cfg['scope']=='east_bay' else BAY_AREA
        active = list(regions)
        budget_hit = False
        try:
            for page in range(1,26):
                next_regions = []
                for region in active:
                    self.update(message='Fetching '+region+' · page '+str(page)+' · '+str(len(rows))+' eligible sales')
                    result = self.request(cfg,'/search',body={'location':region,'status':'sold','page':page,'pageSize':40,'sort':'daysOn','sortAscending':True,'fields':'core,homeType'})
                    items = result.get('searchResults')
                    if not isinstance(items,list):
                        raise ProviderError('The provider search format changed. The saved map is intact.')
                    count = (result.get('searchResultCounts') or {}).get('totalMatchingCount')
                    info = region_counts.setdefault(region,{'total_reported':count,'rows_scanned':0,'pages':0,'capped':False})
                    info['rows_scanned'] += len(items)
                    info['pages'] = page
                    if len(items)==40 and (count is None or page*40<number(count)):
                        next_regions.append(region)
                    for item in items:
                        if not isinstance(item,dict):
                            continue
                        p = item.get('property') or {}
                        if not isinstance(p,dict):
                            continue
                        sqft, beds = number(p.get('livingArea')), number(p.get('bedrooms'))
                        zpid = str(p.get('zpid',''))
                        if not 2000<=sqft<=3000 or beds<2 or not zpid.isdigit() or zpid in seen:
                            continue
                        seen.add(zpid)
                        detail = self.request(cfg,'/property',params={'zpid':zpid,'fields':'zpid,address,location,facts,priceHistory'})
                        row = normalize_property(detail,zpid)
                        if row:
                            rows.append(row)
                active = next_regions
                if not active:
                    break
            for region in active:
                region_counts[region]['capped']=True
        except BudgetReached:
            budget_hit = True
        except Exception as error:
            message = str(error) if isinstance(error,ProviderError) else 'Refresh stopped because the provider response could not be processed. The saved map is intact.'
            self.update(state='error',message=message,finished_at=now())
            return
        if not rows:
            self.update(state='error',message='No verified eligible sales were returned within the request limit. The saved map is intact.',finished_at=now())
            return
        # Partial pulls retain older observations with their original dates/provenance.
        valid, merge = merge_sales(previous.get('sales',previous.get('rows',[])), rows)
        sources = dataset_sources(valid)
        snapshot = dict(previous, updated_at=now(),source=' + '.join(sources) + ' sold-home snapshots',sources=sources,sales=valid,coverage={
            'status':'partial','scope':cfg['scope'],'regions':region_counts,'new_verified_records':len(rows),
            'complete':False,'merge_counts':merge,
            'request_limit_reached':budget_hit,'refresh_calls':self.run_calls,
            'note':'Partial coverage: provider search limits and missing records can omit sales. Older cached observations retain their original sale dates. This is not a census of every home.'})
        snapshot.pop('rows', None)
        self.data.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        atomic_json(self.data/'snapshots'/('sales-'+stamp+'.json'),snapshot)
        atomic_json(self.data/'sales.json',snapshot)
        self.update(state='success',live_verified=True,last_success=snapshot['updated_at'],finished_at=now(),
            message='Fetched '+str(len(rows))+' verified sales; '+str(len(valid))+' eligible sales in the map. '+('Request limit reached; coverage is partial.' if budget_hit else 'Coverage is partial; see source notes.'),
            progress={'completed':self.run_calls,'total':self.run_calls})
