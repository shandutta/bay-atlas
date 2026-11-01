"""Conservative, inspectable comparable-sale estimator. No external dependencies."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import math
import re
from urllib.parse import unquote, urlparse

from confidence import assess_confidence, AUDIT_GRID_METERS, SHADING_GRID_METERS

EARTH_RADIUS_KM = 6371.0088
MAX_RADIUS_KM = 3.0
RADII_KM = (0.75, 1.5, 2.25, 3.0)
# A generous bounding box containing the nine Bay Area counties.
BAY_BOUNDS = {"south": 36.8, "north": 38.9, "west": -123.6, "east": -121.15}
TIME_BUCKETS = ((30, 1.0), (90, 0.8), (183, 0.55), (365, 0.3))


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def two_year_cutoff(as_of: date) -> date:
    """Calendar cutoff, including Feb 29 -> Feb 28 in a non-leap year."""
    try:
        return as_of.replace(year=as_of.year - 2)
    except ValueError:
        return as_of.replace(year=as_of.year - 2, day=28)


def parse_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("Expected an ISO sold date")
    return date.fromisoformat(value[:10])


def finite_number(value) -> float:
    if isinstance(value, bool):
        raise ValueError("Boolean is not numeric data")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Expected a finite number")
    return result


def in_bay_area(lat: float, lng: float) -> bool:
    return (BAY_BOUNDS["south"] <= lat <= BAY_BOUNDS["north"] and
            BAY_BOUNDS["west"] <= lng <= BAY_BOUNDS["east"])


def time_weight(sold_date: date, as_of: date) -> float:
    if sold_date > as_of or sold_date < two_year_cutoff(as_of):
        return 0.0
    age = (as_of - sold_date).days
    for max_age, weight in TIME_BUCKETS:
        if age <= max_age:
            return weight
    return 0.15


def bedroom_weight(comp_beds: float, subject_beds: int) -> float:
    """Prefer similar bedroom counts without asserting a bedroom price premium."""
    if subject_beds >= 3 and comp_beds == 2:
        return 0.75
    return 0.9 ** abs(min(5, comp_beds) - min(5, subject_beds))


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (lat1, lng1, lat2, lng2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def weighted_quantile(values, weights, quantile: float) -> float:
    if not 0 <= quantile <= 1 or not values or len(values) != len(weights):
        raise ValueError("Invalid weighted quantile inputs")
    pairs = sorted((float(value), float(weight)) for value, weight in zip(values, weights) if weight > 0)
    total = sum(weight for _, weight in pairs)
    if not pairs or total <= 0:
        raise ValueError("Quantiles need positive weights")
    threshold = quantile * total
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return pairs[-1][0]


def model_description(as_of: date) -> dict:
    return {
        "as_of": as_of.isoformat(),
        "oldest_sale_date": two_year_cutoff(as_of).isoformat(),
        "time_weights": {"30": 1.0, "90": 0.8, "183": 0.55, "365": 0.3, "2_years": 0.15},
        "time_buckets": [
            {"label": "0–30 days", "max_days": 30, "weight": 1.0},
            {"label": "31–90 days", "max_days": 90, "weight": 0.8},
            {"label": "91–183 days", "max_days": 183, "weight": 0.55},
            {"label": "184–365 days", "max_days": 365, "weight": 0.3},
            {"label": "366 days–2 calendar years", "weight": 0.15},
        ],
        "sqft_min": 2000, "sqft_max": 3000, "minimum_beds": 2,
        "home_type": "Detached single-family homes", "two_bed_weight": 0.75,
        "bedroom_buckets": [2, 3, 4, "5+"], "bedroom_matching_factor": 0.9,
        "bedroom_weight_definition": "0.9 raised to the difference between bedroom buckets (2, 3, 4, 5+), except 2-bedroom comps receive 0.75 for a subject with 3+ bedrooms. A 2-bedroom comp receives full bedroom weight for a 2-bedroom subject.",
        "coverage_audit_grid_m": AUDIT_GRID_METERS,
        "minimum_shading_grid_m": SHADING_GRID_METERS,
        "geographic_resolution_definition": "Diameter enclosing 80% of comparable-sale weight; 250 m display sampling does not imply parcel-level evidence.",
        "maximum_radius_km": MAX_RADIUS_KM, "candidate_radii_km": list(RADII_KM),
        "spatial_kernel": "Gaussian: exp(-0.5 * (distance_km / (radius_km / 2))^2)",
        "size_kernel": "exp(-0.5 * (ln(comp_sqft / subject_sqft) / 0.25)^2)",
        "minimum_comparables": 3, "minimum_effective_comparables": 2,
        "range_definition": "Weighted 20th–80th percentiles of nearby sold price per sqft; descriptive spread, not a confidence interval.",
        "estimate_definition": "Weighted geometric mean, with extreme log prices clipped using weighted median absolute deviation.",
        "limitations": "Does not adjust for condition, renovations, lot size, views, school boundaries or unobserved market changes. Nearby comps can cross neighborhood boundaries. Bedroom choice softly favors similar comps; it does not estimate a causal price premium for adding a bedroom.",
    }


STREET_SUFFIXES = {"street": "st", "avenue": "ave", "road": "rd", "drive": "dr",
                   "lane": "ln", "court": "ct", "boulevard": "blvd", "place": "pl",
                   "terrace": "ter", "circle": "cir", "parkway": "pkwy", "highway": "hwy"}


def source_provider(source_url: str) -> str | None:
    """Provider identity comes from an HTTPS allowlist, never an input label."""
    try:
        parsed = urlparse(source_url)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or "\\" in source_url
                or any(ord(char) < 32 for char in source_url)):
            return None
        for host, label in (("zillow.com", "Zillow"), ("redfin.com", "Redfin")):
            if parsed.hostname == host or (parsed.hostname or "").endswith("." + host):
                return label
    except (ValueError, TypeError):
        pass
    return None


def canonical_address(row: dict, *, omit_suffix=False) -> str:
    """Compare street and city, independent of a provider's display address."""
    street = str(row.get("street_address") or str(row.get("address", "")).split(",", 1)[0]).casefold()
    words = re.findall(r"[a-z0-9]+", street)
    words = [STREET_SUFFIXES.get(word, word) for word in words]
    if omit_suffix and words and words[-1] in set(STREET_SUFFIXES.values()) | {"way"}:
        words.pop()
    city = re.sub(r"[^a-z0-9]", "", str(row.get("city", "")).casefold())
    return " ".join(words) + "|" + city


def source_identity(row: dict) -> str:
    provider = (source_provider(str(row.get("source_url", ""))) or "unknown").casefold()
    identity = str(row.get("source_id") or row.get("id") or canonical_address(row))
    if identity.startswith(provider + ":"):
        identity = identity[len(provider) + 1:]
    return provider + ":" + identity


def same_property(left: dict, right: dict) -> bool:
    if source_identity(left) == source_identity(right):
        return True
    if canonical_address(left) == canonical_address(right):
        return True
    # An omitted street suffix is only accepted with near-identical coordinates.
    left_base, right_base = canonical_address(left, omit_suffix=True), canonical_address(right, omit_suffix=True)
    one_suffix_missing = ((left_base != canonical_address(left)) != (right_base != canonical_address(right)))
    return (one_suffix_missing and left_base == right_base
            and haversine_km(left["lat"], left["lng"], right["lat"], right["lng"]) <= 0.025)


def _market_event_identity(row, *, require_id=False):
    """A quarantine cannot target a provider label or an arbitrary URL path."""
    url = row.get("source_url")
    if not isinstance(url, str):
        return None
    provider = source_provider(url)
    if provider is None:
        return None
    parsed = urlparse(url)
    if (parsed.query or parsed.fragment or parsed.params
            or any(part in {".", ".."} for part in unquote(parsed.path).split("/"))):
        return None
    pattern = (r"/homedetails/(?:[^/]+/)?([0-9]+)_zpid/?" if provider == "Zillow"
               else r"/CA/[^/]+/[^/]+/home/([0-9]+)/?")
    match = re.fullmatch(pattern, parsed.path)
    if not match:
        return None
    value = row.get("source_id", row.get("id"))
    if value is not None:
        value = str(value).removeprefix(provider.lower() + ":")
        if value != match[1]:
            return None
    elif require_id:
        return None
    return provider.lower() + ":" + match[1]


def exclude_market_quarantine(rows: list, quarantine) -> tuple[list, int]:
    """Exclude approved *sale events*, never every sale of a property.

    Date and price must match exactly. Provider URL and ID must agree; a
    cross-provider alias additionally needs a matching address/city within
    25 meters. Malformed or unapproved entries have no filtering effect.
    Audit/evidence fields remain private metadata and are not modified here.
    """
    events = {}
    for entry in quarantine if isinstance(quarantine, list) else []:
        if (not isinstance(entry, dict) or entry.get("approved") is not True
                or entry.get("decision") not in ("quarantine_pending_market_sale_verification",
                                                 "quarantine_ineligible_property_sale")):
            continue
        try:
            identity = _market_event_identity(entry, require_id=True)
            sold = entry.get("sold_date")
            city = entry.get("city")
            if (identity is None or not isinstance(sold, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", sold)
                    or not isinstance(city, str) or not re.search(r"[a-zA-Z0-9]", city)):
                continue
            sold, price = parse_date(sold).isoformat(), finite_number(entry.get("price"))
            if price <= 0:
                continue
            location = None
            try:
                lat, lng = finite_number(entry.get("lat")), finite_number(entry.get("lng"))
                street = entry.get("street_address") or str(entry.get("address", "")).split(",", 1)[0]
                if (isinstance(street, str) and re.match(r"\s*\d+\s+\S", street)
                        and in_bay_area(lat, lng)):
                    location = dict(entry, lat=lat, lng=lng)
            except (ValueError, TypeError, OverflowError):
                pass
            events.setdefault((sold, price), []).append((identity, location))
        except (ValueError, TypeError, OverflowError):
            continue
    if not events:
        return list(rows), 0
    retained = []
    for row in rows:
        excluded = False
        if isinstance(row, dict):
            try:
                candidates = events.get((parse_date(row.get("sold_date")).isoformat(),
                                         finite_number(row.get("price"))), [])
                identity = _market_event_identity(row) if candidates else None
                for quarantined_identity, location in candidates:
                    if identity is None:
                        break
                    if identity == quarantined_identity:
                        excluded = True
                        break
                    if location is not None:
                        lat, lng = finite_number(row.get("lat")), finite_number(row.get("lng"))
                        if (canonical_address(row).split("|", 1)[1] == canonical_address(location).split("|", 1)[1]
                                and haversine_km(lat, lng, location["lat"], location["lng"]) <= 0.025
                                and same_property(dict(row, lat=lat, lng=lng), location)):
                            excluded = True
                            break
            except (ValueError, TypeError, OverflowError, KeyError):
                pass
        if not excluded:
            retained.append(row)
    return retained, len(rows) - len(retained)


def prepare_sales(raw_sales: list, as_of: date | None = None, *, deduplicate=True) -> tuple[list[dict], dict]:
    as_of = as_of or today_utc()
    rejected = Counter()
    candidates = []
    for original in raw_sales:
        if not isinstance(original, dict):
            rejected["invalid_record"] += 1
            continue
        try:
            lat, lng = finite_number(original.get("lat")), finite_number(original.get("lng"))
            price, sqft, beds = (finite_number(original.get(key)) for key in ("price", "sqft", "beds"))
            sold = parse_date(original.get("sold_date"))
        except (ValueError, TypeError, OverflowError):
            rejected["invalid_fields"] += 1
            continue
        home_type = re.sub(r"[^a-z]", "", str(original.get("home_type", "")).casefold())
        if home_type not in {"singlefamily", "singlefamilyresidence", "singlefamilyhome", "detached", "singlefamilydetached"}:
            rejected["not_single_family"] += 1
            continue
        if original.get("is_detached") is False or original.get("is_attached") is True:
            rejected["attached_home"] += 1
            continue
        if not in_bay_area(lat, lng):
            rejected["outside_bay_area"] += 1
            continue
        if price <= 0 or not 2000 <= sqft <= 3000 or beds < 2:
            rejected["outside_home_criteria"] += 1
            continue
        if not time_weight(sold, as_of):
            rejected["outside_date_window"] += 1
            continue
        status = re.sub(r"[^a-z]", "", str(original.get("sale_status", original.get("status", "sold"))).casefold())
        if status not in {"sold", "recentlysold", "closedsale", "closed", "recentlysoldhome"}:
            rejected["not_sold"] += 1
            continue
        source_url = str(original.get("source_url", ""))
        provider = source_provider(source_url)
        if provider is None:
            rejected["invalid_source"] += 1
            continue
        if not str(original.get("address", "")).strip():
            rejected["missing_address"] += 1
            continue
        # Only explicit public schema fields leave the data store.
        row = {key: str(original.get(key, ""))[:1000] for key in ("id", "address", "city", "source_url")}
        row.update(source=provider, lat=lat, lng=lng, price=price, sqft=sqft, beds=beds, sold_date=sold.isoformat(),
                   home_type="singleFamily", price_per_sqft=price / sqft,
                   age_days=(as_of - sold).days, time_weight=time_weight(sold, as_of))
        for key in ("sold_date_precision", "date_evidence", "price_evidence", "street_address", "source_id",
                    "property_type_evidence", "detachment_evidence", "imported_at", "export_sha256", "export_filename"):
            if key in original:
                row[key] = str(original[key])[:1000]
        row["id"] = row["id"] or canonical_address(row)
        if provider == "Redfin" and not row["id"].startswith("redfin:"):
            row["id"] = "redfin:" + row["id"]
        candidates.append(row)
    if not deduplicate:
        return candidates, {"input_count": len(raw_sales), "eligible_count": len(candidates), "excluded": dict(rejected)}
    # A home sold repeatedly is one spatial observation. Retain its latest sale.
    # Stable ordering retains the first observation when equally dated records conflict.
    candidates.sort(key=lambda row: row["sold_date"], reverse=True)
    used_ids, used_addresses = set(), set()
    cleaned = []
    for row in candidates:
        address, identity = canonical_address(row), source_identity(row)
        if identity in used_ids or address in used_addresses:
            rejected["duplicate_home"] += 1
            continue
        used_ids.add(identity)
        used_addresses.add(address)
        cleaned.append(row)
    return cleaned, {"input_count": len(raw_sales), "eligible_count": len(cleaned), "excluded": dict(rejected)}


def merge_sales(previous: list, incoming: list, as_of: date | None = None, *, market_sale_quarantine=None) -> tuple[list[dict], dict]:
    """Retain newer evidence; only explicit rounded prices allow same-date correction."""
    previous, removed_quarantined = exclude_market_quarantine(previous, market_sale_quarantine)
    incoming, excluded_quarantined = exclude_market_quarantine(incoming, market_sale_quarantine)
    current, old_diagnostics = prepare_sales(previous, as_of)
    additions, new_diagnostics = prepare_sales(incoming, as_of, deduplicate=False)
    counts = dict(added=0, updated=0, duplicates=0, stale=0, conflicts=0)
    indexes = ({}, {}, {})

    def keys(row):
        return source_identity(row), canonical_address(row), canonical_address(row, omit_suffix=True)

    def index_row(row, index, remove=False):
        for lookup, key in zip(indexes, keys(row)):
            if remove:
                lookup.get(key, set()).discard(index)
            else:
                lookup.setdefault(key, set()).add(index)

    for index, saved in enumerate(current):
        index_row(saved, index)
    # Newest incoming record first; input order breaks same-date ties conservatively.
    additions.sort(key=lambda row: row["sold_date"], reverse=True)
    for row in additions:
        candidates = set().union(*(lookup.get(key, set()) for lookup, key in zip(indexes, keys(row))))
        matches = [index for index in candidates if same_property(row, current[index])]
        if len(matches) > 1:
            counts["conflicts"] += 1
            continue
        if not matches:
            index_row(row, len(current))
            current.append(row)
            counts["added"] += 1
            continue
        index = matches[0]
        saved = current[index]
        if row["sold_date"] < saved["sold_date"]:
            counts["stale"] += 1
        elif row["sold_date"] > saved["sold_date"]:
            index_row(saved, index, remove=True)
            current[index] = row
            index_row(row, index)
            counts["updated"] += 1
        elif (saved.get("price_evidence") == "Apify Zillow listingSoldPrice display amount; may be rounded"
              and row.get("price_evidence") in {"Apify Zillow listingPriceHistory; exact Sold event price matching dateSold",
                                                "Apify Zillow lastSoldPrice; paired lastSoldDate matches dateSold"}
              and source_provider(row["source_url"]) == "Zillow"
              and source_identity(row) == source_identity(saved)
              and all(row[key] == saved[key] for key in ("sqft", "beds"))):
            # This narrowly upgrades our own documented display-price fallback.
            # Exact saved prices and unrelated same-day conflicts stay protected.
            index_row(saved, index, remove=True)
            current[index] = row
            index_row(row, index)
            counts["updated"] += 1
        elif any(row[key] != saved[key] for key in ("price", "sqft", "beds")):
            counts["conflicts"] += 1
        else:
            counts["duplicates"] += 1
    current.sort(key=lambda row: row["sold_date"], reverse=True)
    return current, dict(counts, removed_quarantined=removed_quarantined, excluded_quarantined=excluded_quarantined,
                         total=len(current), previous_eligible=old_diagnostics["eligible_count"],
                         incoming_eligible=new_diagnostics["eligible_count"], excluded=new_diagnostics["excluded"])


def estimate(sales: list[dict], lat: float, lng: float, sqft: float = 2500, beds: int = 3,
             as_of: date | None = None, prepared: bool = False) -> dict:
    as_of = as_of or today_utc()
    lat, lng, sqft = (finite_number(value) for value in (lat, lng, sqft))
    bed_number = finite_number(beds)
    if not in_bay_area(lat, lng):
        raise ValueError("Choose an address within the San Francisco Bay Area")
    if not 2000 <= sqft <= 3000:
        raise ValueError("Home size must be between 2,000 and 3,000 sqft")
    if bed_number != int(bed_number) or not 2 <= bed_number <= 10:
        raise ValueError("Bedrooms must be a whole number from 2 to 10")
    beds = int(bed_number)
    if not prepared:
        sales, _ = prepare_sales(sales, as_of)
    nearby = []
    # An exact spherical bounding box cheaply rejects distant county records
    # before evaluating trigonometry. Keep haversine as the final inclusion
    # test so prices, ordering, weights, and the 3 km boundary stay unchanged.
    angular_radius = MAX_RADIUS_KM / EARTH_RADIUS_KM
    latitude_delta = math.degrees(angular_radius)
    longitude_delta = math.degrees(math.asin(math.sin(angular_radius) / math.cos(math.radians(lat))))
    for row in sales:
        if abs(row["lat"] - lat) > latitude_delta or abs(row["lng"] - lng) > longitude_delta:
            continue
        distance = haversine_km(lat, lng, row["lat"], row["lng"])
        if distance <= MAX_RADIUS_KM:
            nearby.append((row, distance))
    radius = MAX_RADIUS_KM
    for candidate in RADII_KM:
        if sum(distance <= candidate for _, distance in nearby) >= 8:
            radius = candidate
            break
    nearby = [(row, distance) for row, distance in nearby if distance <= radius]
    comps, weights, log_values = [], [], []
    for row, distance in nearby:
        recency = time_weight(parse_date(row["sold_date"]), as_of)
        spatial = math.exp(-0.5 * (distance / (radius / 2)) ** 2)
        size = math.exp(-0.5 * (math.log(row["sqft"] / sqft) / 0.25) ** 2)
        bedroom = bedroom_weight(row["beds"], beds)
        weight = recency * spatial * size * bedroom
        if weight <= 0:
            continue
        ppsf = row["price"] / row["sqft"]
        comp = dict(row, distance_km=round(distance, 4), distance_miles=round(distance / 1.609344, 4),
                    weight=weight, price_per_sqft=ppsf,
                    weight_components={"recency": recency, "distance": spatial, "size": size, "bedrooms": bedroom})
        comps.append(comp)
        weights.append(weight)
        log_values.append(math.log(ppsf))
    weight_sum = sum(weights)
    effective = weight_sum ** 2 / sum(weight ** 2 for weight in weights) if weight_sum else 0
    comps.sort(key=lambda row: row["weight"], reverse=True)
    for comp in comps:
        comp["normalized_weight"] = comp["weight"] / weight_sum
    result = {
        "status": "insufficient_data", "as_of": as_of.isoformat(), "estimate_ppsf": None,
        "estimated_price": None, "range_ppsf": None, "low_ppsf": None, "high_ppsf": None,
        "confidence": "insufficient", "comparable_count": len(comps), "comp_count": len(comps),
        "effective_sample_count": round(effective, 2), "effective_comp_count": round(effective, 2),
        "radius_km": radius, "radius_miles": round(radius / 1.609344, 2),
        "comparables": comps, "comps": comps,
        "subject": {"lat": lat, "lng": lng, "sqft": sqft, "beds": beds},
        "notes": [], "message": "Not enough comparable sales within 3 km to estimate this address.",
    }
    if len(comps) < 3 or effective < 2:
        result["notes"] = [result["message"], "At least 3 distinct homes and 2 effective comparable sales are required."]
        result["evidence"] = assess_confidence(result, as_of)
        return result
    median = weighted_quantile(log_values, weights, 0.5)
    mad = weighted_quantile([abs(value - median) for value in log_values], weights, 0.5)
    clip_distance = max(0.18, 3 * 1.4826 * mad)
    clipped = [min(median + clip_distance, max(median - clip_distance, value)) for value in log_values]
    ppsf = math.exp(sum(value * weight for value, weight in zip(clipped, weights)) / weight_sum)
    low = math.exp(weighted_quantile(log_values, weights, 0.2))
    high = math.exp(weighted_quantile(log_values, weights, 0.8))
    weighted_age = weighted_quantile([comp["age_days"] for comp in comps], [comp["weight"] for comp in comps], 0.5)
    nearest = min(comp["distance_km"] for comp in comps)
    limited = len(comps) < 8 or effective < 5 or weighted_age > 183 or nearest > 1.0
    outlier_count = sum(a != b for a, b in zip(log_values, clipped))
    result.update(
        status="limited" if limited else "ok", confidence="limited" if limited else "moderate",
        estimate_ppsf=round(ppsf, 2), estimated_price=round(ppsf * sqft),
        range_ppsf={"low": round(low, 2), "high": round(high, 2)},
        low_ppsf=round(low, 2), high_ppsf=round(high, 2), outlier_count=outlier_count,
        message="Limited nearby sale evidence; treat this estimate cautiously." if limited else "Estimate based on recent, nearby comparable sales.",
    )
    result["notes"] = [
        result["message"],
        "The range is the weighted 20th–80th percentile spread of sold comparables, not a statistical confidence interval.",
        "Condition, renovations, lot, views and school boundaries are not modeled. Bedroom choice softly favors comps with similar bedroom counts, with all 5+ counts grouped together. It does not estimate the price effect of adding a bedroom.",
        "Recent sales are weighted more heavily; older prices are not adjusted by a market index.",
    ]
    if outlier_count:
        result["notes"].append(f"Reduced the influence of {outlier_count} extreme comparable price{'s' if outlier_count != 1 else ''}.")
    result["evidence"] = assess_confidence(result, as_of)
    return result
