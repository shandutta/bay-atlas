"""Validate a manually downloaded Redfin sold-home CSV. No network or writes.

The observed export has explicit SOLD DATE, STATUS and SALE TYPE fields. Blank
sale dates are never inferred, and the PRICE field is used only for Sold rows.
Single Family Residential is Redfin's classification; the export does not
separately establish physical detachment. The caller owns cross-source merging
and atomic publication. Importing a CSV does not establish a live refresh feed.
"""
from __future__ import annotations

from collections import Counter
import csv
from datetime import date, datetime, timezone
import hashlib
import io
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

from model import finite_number, in_bay_area, today_utc, two_year_cutoff

MAX_BYTES = 8 * 1024 * 1024
URL_HEADER = 'URL (SEE https://www.redfin.com/buy-a-home/comparative-market-analysis FOR INFO ON PRICING)'
REQUIRED_HEADERS = {'SALE TYPE', 'SOLD DATE', 'PROPERTY TYPE', 'ADDRESS', 'CITY',
                    'STATE OR PROVINCE', 'ZIP OR POSTAL CODE', 'PRICE', 'BEDS',
                    'SQUARE FEET', 'STATUS', 'LATITUDE', 'LONGITUDE', URL_HEADER}
MONTHS = {name.casefold(): i + 1 for i, name in enumerate((
    'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
    'September', 'October', 'November', 'December'))}


class RedfinCSVError(ValueError):
    """The input is not the expected, bounded Redfin export format."""


def sold_date(value: str) -> date:
    """Parse the observed English month-name format without locale dependence."""
    match = re.fullmatch(r'([A-Za-z]+)-(\d{1,2})-(\d{4})', value.strip())
    if not match or match[1].casefold() not in MONTHS:
        raise ValueError('Unrecognized Redfin sold date')
    return date(int(match[3]), MONTHS[match[1].casefold()], int(match[2]))


def property_url(value: str) -> tuple[str, str]:
    if any(ord(c) < 32 for c in value) or '\\' in value:
        raise ValueError('Unsafe property URL')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in {'redfin.com', 'www.redfin.com'}
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError('Invalid property source')
    match = re.fullmatch(r'/CA/[^/]+/[^/]+/home/(\d+)/?', parsed.path)
    if not match:
        raise ValueError('Expected a California Redfin property URL')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')), match[1]


def parse_csv(payload: bytes | str, *, as_of: date | None = None,
              export_filename: str = '', imported_at: str | None = None) -> dict:
    """Return eligible raw-schema sales plus audit data, without side effects."""
    as_of = as_of or today_utc()
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    if not isinstance(payload, bytes) or len(payload) > MAX_BYTES:
        raise RedfinCSVError('Redfin CSV exceeds the local input limit or has invalid type.')
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise RedfinCSVError('Redfin CSV must be UTF-8.') from None
    imported_at = imported_at or datetime.now(timezone.utc).isoformat()
    try:
        imported_time = datetime.fromisoformat(imported_at.replace('Z', '+00:00'))
        if imported_time.tzinfo is None:
            raise ValueError('Missing timezone')
    except (ValueError, TypeError, AttributeError):
        raise RedfinCSVError('The import timestamp must include a timezone.') from None
    digest = hashlib.sha256(payload).hexdigest()
    excluded, sales, all_dates = Counter(), [], []
    count = 0
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
        headers = reader.fieldnames or []
        if len(set(headers)) != len(headers) or not REQUIRED_HEADERS.issubset(headers):
            raise RedfinCSVError('The CSV is missing required Redfin columns or repeats headers.')
        for raw in reader:
            count += 1
            if str(raw.get('SALE TYPE', '')).startswith('In accordance with local MLS rules,'):
                excluded['mls_notice'] += 1
                continue
            if None in raw or any(raw.get(k) is None for k in REQUIRED_HEADERS):
                excluded['malformed_row'] += 1
                continue
            row = {key: value.strip() if isinstance(value, str) else value for key, value in raw.items()}
            if not row['SOLD DATE']:
                excluded['missing_sold_date'] += 1
                continue
            try:
                when = sold_date(row['SOLD DATE'])
            except ValueError:
                excluded['invalid_sold_date'] += 1
                continue
            all_dates.append(when.isoformat())
            if row['SALE TYPE'] != 'PAST SALE' or row['STATUS'] != 'Sold':
                excluded['not_explicit_closed_sale'] += 1
                continue
            if when < two_year_cutoff(as_of) or when > as_of:
                excluded['outside_date_window'] += 1
                continue
            if row['PROPERTY TYPE'] != 'Single Family Residential':
                excluded['not_single_family'] += 1
                continue
            try:
                price, sqft, beds, lat, lng = (finite_number(row[key]) for key in (
                    'PRICE', 'SQUARE FEET', 'BEDS', 'LATITUDE', 'LONGITUDE'))
            except (ValueError, TypeError, OverflowError):
                excluded['invalid_numeric_fields'] += 1
                continue
            if price <= 0 or not 2000 <= sqft <= 3000 or beds < 2 or beds != int(beds):
                excluded['outside_home_criteria'] += 1
                continue
            if row['STATE OR PROVINCE'] != 'CA' or not in_bay_area(lat, lng):
                excluded['outside_bay_area'] += 1
                continue
            if not row['ADDRESS'] or not row['CITY']:
                excluded['missing_address'] += 1
                continue
            try:
                source_url, source_id = property_url(row[URL_HEADER])
            except ValueError:
                excluded['invalid_redfin_source'] += 1
                continue
            address = f"{row['ADDRESS']}, {row['CITY']}, CA"
            if row['ZIP OR POSTAL CODE']:
                address += ' ' + row['ZIP OR POSTAL CODE']
            sales.append({
                'id': 'redfin:' + source_id, 'source_id': source_id, 'source': 'Redfin',
                'address': address, 'street_address': row['ADDRESS'], 'city': row['CITY'],
                'state': 'CA', 'zip': row['ZIP OR POSTAL CODE'], 'lat': lat, 'lng': lng,
                'price': price, 'sqft': sqft, 'beds': beds, 'sold_date': when.isoformat(),
                'sale_status': 'sold', 'home_type': 'singleFamily',
                'source_url': source_url, 'property_type_evidence': row['PROPERTY TYPE'],
                'detachment_evidence': 'Not separately reported in the Redfin CSV',
                'sold_date_precision': 'day',
                'date_evidence': 'Redfin CSV explicit SOLD DATE, STATUS Sold, SALE TYPE PAST SALE',
                'price_evidence': 'Redfin CSV PRICE for explicit closed sale; not rounded $/SQUARE FEET',
                'listing_source': row.get('SOURCE', ''), 'mls_number': row.get('MLS#', ''),
                'imported_at': imported_at, 'export_sha256': digest,
                'export_filename': Path(export_filename).name,
            })
    except csv.Error:
        raise RedfinCSVError('Redfin CSV could not be parsed.') from None
    by_id = Counter(row['id'] for row in sales)
    return {
        'source': 'Redfin manually downloaded sold-home CSV', 'sales': sales,
        'imported_at': imported_at, 'export_sha256': digest,
        'export_filename': Path(export_filename).name, 'byte_count': len(payload),
        'as_of': as_of.isoformat(), 'oldest_sale_date': two_year_cutoff(as_of).isoformat(),
        'diagnostics': {'input_count': count, 'eligible_before_dedup': len(sales),
                        'excluded': dict(excluded),
                        'duplicate_source_ids': sum(n - 1 for n in by_id.values() if n > 1)},
        'observed_export_date_range': {'oldest': min(all_dates) if all_dates else None,
                                       'newest': max(all_dates) if all_dates else None},
        'coverage': {'complete': False, 'refresh_mode': 'manual_csv',
                     'city_counts': dict(Counter(row['city'] for row in sales))},
        'warnings': [
            'Manual CSV snapshot; no automated Redfin refresh connection has been verified.',
            'Redfin notes that local MLS rules omit some listings from exports.',
            'Single Family Residential is the source classification; physical detachment is not separately reported.',
        ],
    }


def read_csv(path: str | Path, *, as_of: date | None = None,
             imported_at: str | None = None) -> dict:
    """Read at most MAX_BYTES+1 and pass the bytes to the side-effect-free parser."""
    path = Path(path)
    with path.open('rb') as stream:
        payload = stream.read(MAX_BYTES + 1)
    return parse_csv(payload, as_of=as_of, export_filename=path.name, imported_at=imported_at)
