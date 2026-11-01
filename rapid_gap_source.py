"""Strict offline adapter for US Real Estate API's detailed Zillow sold results.

The adapter consumes saved responses only. It makes no network calls, stores no
credentials, and never exports contact details, photos, descriptions or nearby
homes. Prices require an exact dated Sold event; the generic price/Zestimate
fields are not used. Full details and explicit detached-property evidence are
required for new comparable sales.
"""
from collections import Counter
from datetime import date, datetime
import re
from urllib.parse import urlparse

from model import finite_number, prepare_sales, today_utc

PROVIDER = 'Zillow via US Real Estate API'


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?', value):
        raise ValueError('Missing explicit ISO sale date')
    if 'T' in value:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    return date.fromisoformat(value[:10]).isoformat()


def _exact_price(record, sold):
    history = record.get('priceHistory')
    if not isinstance(history, list):
        raise ValueError('Missing completed-sale history')
    matches = set()
    for event in history:
        if not isinstance(event, dict):
            raise ValueError('Malformed price history')
        if str(event.get('event', '')).strip().casefold() != 'sold' or event.get('postingIsRental') is True:
            continue
        event_date = _date(event.get('date'))
        if event_date > sold:
            raise ValueError('Newer completed sale contradicts sold date')
        if event_date == sold:
            price = finite_number(event.get('price'))
            if price <= 0:
                raise ValueError('Invalid completed sale price')
            matches.add(price)
    if len(matches) != 1:
        raise ValueError('Missing or conflicting completed sale price')
    price = matches.pop()
    if record.get('lastSoldDate') is not None:
        paired_date = _date(record['lastSoldDate'])
        if paired_date > sold:
            raise ValueError('Newer paired sale date')
        if paired_date == sold and finite_number(record.get('lastSoldPrice')) != price:
            raise ValueError('Conflicting paired sale price')
    return price


def _attached_evidence(record, facts):
    for label, source in [('record', record), ('facts', facts)]:
        for field in ['isAttached', 'is_attached', 'hasAttachedProperty']:
            if source.get(field) is True:
                return 'Zillow ' + label + '.' + field + ' true'
        for field in ['isDetached', 'is_detached']:
            if source.get(field) is False:
                return 'Zillow ' + label + '.' + field + ' false'
        subtypes = str(source.get('propertySubType', source.get('propertySubtype', '')))
        if re.search(r'\b(attached|townhouse|townhome|condo|condominium|apartment)\b', subtypes, re.I):
            return 'Zillow ' + label + ' property subtype explicitly indicates an attached home'
    return None


def _normalize(record, as_of, *, allow_attached=False):
    if not isinstance(record, dict):
        return None, 'invalid_record', None
    if record.get('homeStatus') not in {'RECENTLY_SOLD', 'SOLD', 'sold'}:
        return None, 'not_sold', None
    if record.get('detailsLevel') != 'full':
        return None, 'missing_full_details', None
    if record.get('homeType') != 'SINGLE_FAMILY':
        return None, 'not_single_family', None
    if record.get('state') != 'CA':
        return None, 'outside_california', None
    if record.get('currency') is not None and record.get('currency') != 'USD':
        return None, 'invalid_currency', None
    zpid, url = str(record.get('zpid', '')), record.get('url')
    try:
        parsed = urlparse(url)
        identity = re.search(r'/(\d+)_zpid/?$', parsed.path)
        if (not zpid.isdigit() or len(zpid) > 30 or not isinstance(url, str)
                or parsed.scheme != 'https' or parsed.hostname not in {'www.zillow.com', 'zillow.com'}
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or not parsed.path.startswith('/homedetails/') or not identity
                or identity.group(1) != zpid or '\\' in url or any(ord(c) < 32 for c in url)):
            return None, 'invalid_source', None
    except (TypeError, ValueError, AttributeError):
        return None, 'invalid_source', None
    facts = record.get('facts')
    if not isinstance(facts, dict):
        return None, 'missing_property_facts', None
    attached = _attached_evidence(record, facts)
    if attached and not allow_attached:
        return None, 'attached_home', attached
    if not attached and facts.get('hasAttachedProperty') is not False:
        return None, 'missing_explicit_detachment', None
    street, city = record.get('street'), record.get('city')
    if not isinstance(street, str) or not street.strip() or not isinstance(city, str) or not city.strip():
        return None, 'missing_address', None
    if record.get('unit') or facts.get('unit') or re.search(r'\b(?:APT|UNIT|STE|SUITE)\s+\w|#\s*\w', street, re.I):
        return None, 'attached_home', None
    try:
        sold = _date(record.get('soldDate'))
        price = _exact_price(record, sold)
        lat, lng = finite_number(record.get('latitude')), finite_number(record.get('longitude'))
        sqft, beds = finite_number(record.get('livingArea')), finite_number(record.get('bedrooms'))
    except (TypeError, ValueError, OverflowError):
        return None, 'invalid_sold_fields', None
    row = {
        'id': zpid, 'source_id': zpid, 'source_url': url, 'street_address': street.strip(),
        'address': ', '.join(str(value) for value in [street.strip(), city.strip(), 'CA', record.get('zip')] if value),
        'city': city.strip(), 'lat': lat, 'lng': lng, 'price': price, 'sqft': sqft, 'beds': beds,
        'sold_date': sold, 'sale_status': 'sold', 'home_type': 'singleFamily',
        'date_evidence': 'US Real Estate API Zillow soldDate; explicit sold date',
        'price_evidence': 'US Real Estate API Zillow priceHistory; exact Sold event price matching soldDate',
        'property_type_evidence': 'Zillow homeType SINGLE_FAMILY',
        'detachment_evidence': attached or 'Zillow facts.hasAttachedProperty false; single-family classification',
    }
    valid, diagnostics = prepare_sales([row], as_of=as_of)
    if not valid:
        return None, next(iter(diagnostics['excluded']), 'invalid_record'), None
    return valid[0], None, attached


def normalize_property(record, *, as_of=None):
    return _normalize(record, as_of or today_utc())[0]


def normalize_records(items, *, as_of=None):
    """Return only validated schema fields plus aggregate rejection diagnostics."""
    if not isinstance(items, list):
        raise ValueError('Detailed property response must be a list')
    as_of = as_of or today_utc()
    candidates, excluded, attached_exclusions = [], Counter(), {}
    for record in items:
        row, reason, attachment = _normalize(record, as_of)
        if row:
            candidates.append(row)
            continue
        excluded[reason] += 1
        if reason == 'attached_home' and attachment:
            verified, _, _ = _normalize(record, as_of, allow_attached=True)
            if verified:
                fields = ['id', 'source_id', 'source_url', 'street_address', 'city', 'address', 'lat', 'lng', 'sold_date']
                attached_exclusions[verified['source_id']] = {
                    **{key: verified[key] for key in fields}, 'reason': 'explicit_attached_home',
                    'attachment_evidence': attachment,
                }
    sales, deduplication = prepare_sales(candidates, as_of=as_of)
    excluded.update(deduplication['excluded'])
    return {'sales': sales, 'diagnostics': {'input_count': len(items), 'eligible_count': len(sales),
            'excluded': dict(excluded), 'explicit_attached_exclusions': list(attached_exclusions.values())}}
