"""Private local suggestions plus bounded Photon address autocomplete.

Photon explicitly supports search-as-you-type and reasonable public API usage:
https://github.com/komoot/photon and docs/api-v1.md. Public Nominatim is not used.
Queries are cached in bounded process memory only, never written to disk/logs.
"""
from collections import OrderedDict
from bisect import bisect_left
import json
import re
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener

from model import BAY_BOUNDS, STREET_SUFFIXES, finite_number, in_bay_area

MIN_QUERY_LENGTH, MAX_QUERY_LENGTH, RESULT_LIMIT = 6, 120, 5
MAX_CACHE_ENTRIES, CACHE_TTL_SECONDS = 256, 24 * 60 * 60
NEGATIVE_CACHE_TTL_SECONDS = 5 * 60
MAX_RESPONSE_BYTES, REQUEST_TIMEOUT, REQUEST_INTERVAL = 500_000, 8, 1.05
PROVIDER = 'Photon / OpenStreetMap'
ATTRIBUTION = {'label': '© OpenStreetMap contributors', 'url': 'https://www.openstreetmap.org/copyright'}
NOTICE = 'Address text is sent to Photon (komoot) for suggestions; saved-sale matches are searched locally.'


def normalize_query(query):
    if not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in query):
        raise ValueError('Enter 6–120 characters of a street address or place name.')
    query = ' '.join(query.split())
    if not MIN_QUERY_LENGTH <= len(query) <= MAX_QUERY_LENGTH:
        raise ValueError('Type at least 6 characters for address suggestions.')
    if not any(char.isalnum() for char in query):
        raise ValueError('Enter part of a street address or place name.')
    return query


def _words(value):
    text = ''.join(char for char in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(char))
    return [STREET_SUFFIXES.get(word, word) for word in re.findall(r'[a-z0-9]+', text)]


def _text(value, limit=200):
    return ' '.join(str(value).split())[:limit] if isinstance(value, (str, int)) else ''


def precise_address_matches(query, results):
    """Only reuse complete house-and-street matches for submitted searches.

    A provider can ignore a house number and return the street, or return a
    nearby numbered property. Neither is evidence for the requested home.
    Locality qualifiers must also match; an address shared by multiple cities
    remains multiple results for the caller to present to the user.
    """
    tokens = _words(query)
    found = []
    for row in results:
        if not row.get('address_level'):
            continue
        street = _words(row.get('street_address', ''))
        if (len(street) < 2 or not street[0][0].isdigit()
                or tokens[:len(street)] != street):
            continue
        # Match the entire city component, not a subsequence: "San Francisco"
        # must not resolve to an address in "South San Francisco".
        parts = row.get('display_name', '').split(',')
        city = _words(parts[1]) if len(parts) > 1 else []
        region = _words(' '.join(parts[2:]))
        qualifiers = tokens[len(street):]
        if city and qualifiers[:len(city)] == city:
            qualifiers = qualifiers[len(city):]
        qualifiers = ['ca' if word == 'california' else word for word in qualifiers]
        remaining = iter(region)
        if not all(any(word == item for item in remaining) for word in qualifiers):
            continue
        try:
            if not in_bay_area(finite_number(row.get('lat')), finite_number(row.get('lng'))):
                continue
        except (ValueError, TypeError):
            continue
        found.append(dict(row))
    return found


def normalize_photon(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('features'), list):
        raise ValueError('Invalid address suggestion response')
    results, seen = [], set()
    for feature in payload['features'][:50]:
        try:
            if not isinstance(feature, dict):
                continue
            geometry, props = feature.get('geometry'), feature.get('properties')
            if not isinstance(geometry, dict) or not isinstance(props, dict) or geometry.get('type') != 'Point':
                continue
            coords = geometry.get('coordinates')
            if not isinstance(coords, list) or len(coords) != 2:
                continue
            lng, lat = map(finite_number, coords)
            if not in_bay_area(lat, lng):
                continue
            if props.get('countrycode', 'US').upper() != 'US':
                continue
            if props.get('state') and str(props['state']).casefold() not in {'ca', 'california'}:
                continue
            house, street, name = (_text(props.get(key)) for key in ('housenumber', 'street', 'name'))
            city = _text(props.get('city') or props.get('locality') or props.get('district') or props.get('county'))
            if house and street:
                first, precision = house + ' ' + street, 'Mapped street address'
            elif street or name:
                first = name or street
                precision = 'Approximate street or place location'
            else:
                continue
            display = ', '.join(part for part in (first, city, 'CA ' + _text(props.get('postcode'), 20)) if part).strip()
            identity = tuple(_words(display))
            if identity in seen:
                continue
            seen.add(identity)
            results.append({'lat': lat, 'lng': lng, 'display_name': display,
                            'source': PROVIDER, 'precision': precision,
                            'address_level': bool(house and street),
                            'street_address': house + ' ' + street if house and street else ''})
            if len(results) == RESULT_LIMIT:
                break
        except (ValueError, TypeError, AttributeError):
            continue
    return results


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError('Address service redirected unexpectedly')


class AddressSuggestions:
    def __init__(self, snapshot, *, opener=None, clock=None, sleep=None):
        self.snapshot = snapshot
        self._open = opener or build_opener(_NoRedirect()).open
        self._clock, self._sleep = clock or time.monotonic, sleep or time.sleep
        self._request_lock, self._cache_lock = threading.Lock(), threading.Lock()
        self._cache = OrderedDict()
        self._last_request = None
        self._indexed_sales, self._index = None, []
        self._word_rows, self._sorted_words = {}, []

    def _local(self, query, *, precise_only=False):
        sales = self.snapshot().get('sales', [])
        with self._cache_lock:
            if sales is not self._indexed_sales:
                index = []
                for row in sales:
                    try:
                        address = _text(row.get('address'), 300)
                        lat, lng = finite_number(row.get('lat')), finite_number(row.get('lng'))
                        if address and in_bay_area(lat, lng):
                            street = _text(row.get('street_address'), 300) or address.split(',', 1)[0]
                            index.append((_words(address), {'lat': lat, 'lng': lng, 'display_name': address,
                                         'source': 'Local sale records', 'precision': 'Saved property location',
                                         'address_level': True, 'street_address': street}))
                    except (ValueError, TypeError, AttributeError):
                        continue
                # Full-word postings avoid rescanning every saved address on
                # each keystroke. Sorted words also support partial streets
                # without storing every possible prefix in memory.
                word_rows = {}
                for position, (words, _) in enumerate(index):
                    for word in set(words):
                        word_rows.setdefault(word, set()).add(position)
                self._index, self._indexed_sales = index, sales
                self._word_rows, self._sorted_words = word_rows, sorted(word_rows)
            index, word_rows, sorted_words = self._index, self._word_rows, self._sorted_words
        tokens = _words(query)
        if not tokens:
            return []
        candidates = None
        for token in tokens:
            if token.isdigit():
                matches = word_rows.get(token, set())
            else:
                matches = set()
                position = bisect_left(sorted_words, token)
                while position < len(sorted_words) and sorted_words[position].startswith(token):
                    matches.update(word_rows[sorted_words[position]])
                    position += 1
            candidates = set(matches) if candidates is None else candidates.intersection(matches)
            if not candidates:
                return []
        found = []
        for position in sorted(candidates):
            words, item = index[position]
            starts = words[:len(tokens)] == tokens
            found.append((not starts, len(words), item['display_name'], item))
        found.sort(key=lambda entry: entry[:3])
        results, seen = [], set()
        for *_, item in found:
            if precise_only and not precise_address_matches(query, [item]):
                continue
            key = tuple(_words(item['display_name']))
            if key not in seen:
                results.append(dict(item))
                seen.add(key)
                if len(results) == RESULT_LIMIT:
                    break
        return results

    def _cached(self, key):
        with self._cache_lock:
            item = self._cache.get(key)
            ttl = CACHE_TTL_SECONDS if item and item['results'] else NEGATIVE_CACHE_TTL_SECONDS
            if item and 0 <= self._clock() - item['at'] < ttl:
                self._cache.move_to_end(key)
                return [dict(row) for row in item['results']]
            self._cache.pop(key, None)
        return None

    def _fetch(self, query):
        params = [('q', query), ('bbox', ','.join(str(BAY_BOUNDS[key]) for key in ('west', 'south', 'east', 'north'))),
                  ('countrycode', 'US'), ('limit', str(RESULT_LIMIT)), ('lang', 'en'),
                  ('layer', 'house'), ('layer', 'street')]
        request = Request('https://photon.komoot.io/api/?' + urlencode(params),
                          headers={'Accept': 'application/json', 'Accept-Encoding': 'identity',
                                   'User-Agent': 'BayAtlasPrivateHomeMap/1.0 (personal Bay Area address search)'})
        with self._open(request, timeout=REQUEST_TIMEOUT) as response:
            if hasattr(response, 'geturl'):
                url = urlparse(response.geturl())
                if url.scheme != 'https' or url.hostname != 'photon.komoot.io':
                    raise ValueError('Unexpected address service')
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError('Address response too large')
        return normalize_photon(json.loads(body))

    def search(self, query, *, wait_timeout=1.05, precise_only=False):
        query = normalize_query(query)
        key = query.casefold()
        local = self._local(query, precise_only=precise_only)
        base = {'provider': PROVIDER, 'attribution': dict(ATTRIBUTION), 'notice': NOTICE}
        if ((not precise_only and len(local) >= RESULT_LIMIT)
                or any(_words(query) == _words(row['display_name']) for row in local)):
            return dict(base, results=local, cached=True, remote_status='not_needed', status='matched')
        remote = self._cached(key)
        cached, remote_status = remote is not None, 'ok'
        if remote is None:
            # Bounded waiting avoids a queue of stale keystrokes during a slow outage.
            if not self._request_lock.acquire(timeout=wait_timeout):
                return dict(base, results=local, cached=False, remote_status='busy', retry_after_ms=1100,
                            status='matched' if local else 'busy', message='Address suggestions are busy. Try again shortly.')
            try:
                remote = self._cached(key)
                if remote is not None:
                    cached = True
                else:
                    if self._last_request is not None:
                        self._sleep(max(0, REQUEST_INTERVAL - (self._clock() - self._last_request)))
                    self._last_request = self._clock()
                    try:
                        remote = self._fetch(query)
                    except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError):
                        remote, remote_status = [], 'unavailable'
                    if remote_status == 'ok':
                        with self._cache_lock:
                            self._cache[key] = {'at': self._clock(), 'results': remote}
                            self._cache.move_to_end(key)
                            while len(self._cache) > MAX_CACHE_ENTRIES:
                                self._cache.popitem(last=False)
            finally:
                self._request_lock.release()
        if precise_only:
            remote = precise_address_matches(query, remote)
        results, seen = [], set()
        for item in [*local, *remote]:
            identity = tuple(_words(item['display_name']))
            if identity not in seen:
                results.append(item)
                seen.add(identity)
                if len(results) == RESULT_LIMIT:
                    break
        status = 'matched' if results else ('no_match' if remote_status == 'ok' else remote_status)
        message = ('Address suggestions are temporarily unavailable. Try again shortly or submit a full address.'
                   if remote_status == 'unavailable' else
                   'No address suggestions found. Add a city or ZIP code, or submit the full address.' if not results else '')
        return dict(base, results=results, cached=cached, remote_status=remote_status, status=status, message=message)
