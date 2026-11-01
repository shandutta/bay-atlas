"""Inspectable evidence diagnostics for a neighborhood price estimate.

These gates are acquisition targets, not calibrated probabilities or prediction
intervals. Nearby homes can share unobserved features and source selection bias.
"""
from collections import defaultdict
from datetime import date, datetime, timezone
import math

VERSION = 1
AUDIT_GRID_METERS = 500
SHADING_GRID_METERS = 250
SPATIAL_BLOCK_METERS = 250
TARGET = {
    'effective_homes': 12, 'recent_183_days': 4, 'effective_spatial_blocks': 4,
    'balanced_quadrants': 3, 'weight_radius_80_km': 1.5,
    'median_age_days': 183, 'log_standard_error_proxy': .08,
    'max_block_log_shift': .10,
}


def _quantile(values, weights, q):
    total = sum(weights)
    threshold, cumulative = q * total, 0.0
    ordered = sorted(zip(values, weights))
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Evidence fields must be finite')
    return number


def assess_confidence(result, as_of=None):
    """Assess an already-selected estimate without changing its price or radius.

    Recompute sale ages at the evaluation date. The 250 m spatial blocks reduce
    false precision from clustered homes; their effective count is still not a
    count of statistically independent neighborhoods. The block sensitivity is for the raw weighted log mean, rather than the
    clipped point estimator. The block standard-error proxy omits systematic bias and is deliberately not a confidence interval.
    """
    as_of = _date(as_of or result.get('as_of') or datetime.now(timezone.utc).date())
    try:
        oldest = as_of.replace(year=as_of.year - 2)
    except ValueError:
        oldest = as_of.replace(year=as_of.year - 2, day=28)
    subject = result.get('subject', {})
    lat, lng = _finite(subject['lat']), _finite(subject['lng'])
    observations = []
    skipped = 0
    for comp in result.get('comparables', []):
        try:
            weight = _finite(comp['weight'])
            ppsf = _finite(comp['price_per_sqft'] if 'price_per_sqft' in comp
                           else _finite(comp['price']) / _finite(comp['sqft']))
            comp_lat, comp_lng = _finite(comp['lat']), _finite(comp['lng'])
            distance = _finite(comp['distance_km'])
            sold = _date(comp['sold_date'])
            age = (as_of - sold).days
            if weight <= 0 or ppsf <= 0 or distance < 0 or age < 0 or sold < oldest:
                raise ValueError('Invalid comparable evidence')
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
            skipped += 1
            continue
        observations.append((weight, math.log(ppsf), comp_lat, comp_lng, distance, age))
    output = {
        'version': VERSION, 'as_of': as_of.isoformat(),
        'evidence_tier': 'insufficient', 'target_met': False,
        'is_calibrated_probability': False,
        'metrics': {}, 'reasons': [], 'target': dict(TARGET),
        'resolution': {
            'audit_grid_m': AUDIT_GRID_METERS, 'shading_grid_m': SHADING_GRID_METERS,
            'spatial_block_m': SPATIAL_BLOCK_METERS,
            'support_radius_80_m': None, 'support_diameter_80_m': None,
            'meaning': 'The 80%-weight support diameter describes the area supplying evidence. Shading uses a minimum nominal 250 m grid and coarser nested levels when zoomed out; smooth display pixels do not add geographic precision.',
        },
        'limitations': 'Evidence quality is not a probability that a home will sell within a price range. The log standard-error proxy omits source selection bias, market shifts, and unmodeled home differences; spatial blocks are not proven independent.',
    }
    if not observations:
        output['reasons'] = ['no_valid_comparables']
        return output
    total = sum(o[0] for o in observations)
    weights = [o[0] / total for o in observations]
    logs = [o[1] for o in observations]
    neff = 1 / sum(w * w for w in weights)
    mean = sum(w * v for w, v in zip(weights, logs))
    median = _quantile(logs, weights, .5)
    sigma = 1.4826 * _quantile([abs(v - median) for v in logs], weights, .5)
    blocks, quadrants = defaultdict(lambda: [0.0, 0.0]), defaultdict(float)
    # Fixed local equirectangular metric grid; adequate for grouping, never used
    # to replace the estimator's great-circle distances.
    longitude_scale = 111320 * math.cos(math.radians(37.8))
    for row, w in zip(observations, weights):
        _, log_price, comp_lat, comp_lng, _, _ = row
        block = (math.floor(comp_lng * longitude_scale / SPATIAL_BLOCK_METERS),
                 math.floor(comp_lat * 111195 / SPATIAL_BLOCK_METERS))
        blocks[block][0] += w
        blocks[block][1] += w * (log_price - mean)
        quadrants[(comp_lat >= lat, comp_lng >= lng)] += w
    block_eff = 1 / sum(b[0] ** 2 for b in blocks.values())
    count_blocks = len(blocks)
    block_se = (math.sqrt(count_blocks / (count_blocks - 1) * sum(b[1] ** 2 for b in blocks.values()))
                if count_blocks > 1 else 1.0)
    se_proxy = max(block_se, sigma / math.sqrt(max(1, min(neff, block_eff))))
    # Removing a whole block tests the raw weighted log mean's dependence on
    # one nearby cluster. It is not an exact refit of the clipped estimator.
    max_shift = max((abs(b[1] / (1 - b[0])) for b in blocks.values() if b[0] < .999), default=1.0)
    r80 = _quantile([o[4] for o in observations], weights, .8)
    median_age = _quantile([o[5] for o in observations], weights, .5)
    metrics = {
        'homes': len(observations), 'effective_homes': neff,
        'recent_90_days': sum(o[5] <= 90 for o in observations),
        'recent_183_days': sum(o[5] <= 183 for o in observations),
        'recent_183_weight_share': sum(w for o, w in zip(observations, weights) if o[5] <= 183),
        'spatial_blocks': count_blocks, 'effective_spatial_blocks': block_eff,
        'balanced_quadrants': sum(w >= .10 for w in quadrants.values()),
        'median_age_days': median_age, 'weight_radius_80_km': r80,
        'nearest_km': min(o[4] for o in observations), 'largest_home_weight': max(weights),
        'robust_log_spread': sigma, 'log_standard_error_proxy': se_proxy,
        'max_block_log_shift': max_shift, 'ignored_invalid_comparables': skipped,
    }
    checks = {
        'too_few_effective_homes': neff >= TARGET['effective_homes'],
        'too_few_recent_sales': metrics['recent_183_days'] >= TARGET['recent_183_days'],
        'spatially_concentrated': block_eff >= TARGET['effective_spatial_blocks'],
        'one_sided_evidence': metrics['balanced_quadrants'] >= TARGET['balanced_quadrants'],
        'evidence_too_distant': r80 <= TARGET['weight_radius_80_km'],
        'older_evidence': median_age <= TARGET['median_age_days'],
        'high_sampling_variability': se_proxy <= TARGET['log_standard_error_proxy'],
        'sensitive_to_one_block': max_shift <= TARGET['max_block_log_shift'],
    }
    enough = len(observations) >= 3 and neff >= 2 and result.get('estimate_ppsf') is not None
    target_met = enough and not skipped and all(checks.values())
    usable = (enough and neff >= 8 and metrics['recent_183_days'] >= 3 and block_eff >= 3
              and r80 <= 2 and median_age <= 183 and se_proxy <= .12 and max_shift <= .15)
    output.update(evidence_tier='target' if target_met else 'usable' if usable else 'limited' if enough else 'insufficient',
                  target_met=target_met, metrics={key: round(value, 6) if isinstance(value, float) else value
                                                for key, value in metrics.items()},
                  reasons=[key for key, passed in checks.items() if not passed])
    if not enough:
        output['reasons'].insert(0, 'minimum_estimate_support_not_met')
    if skipped:
        output['reasons'].append('invalid_comparables_ignored')
    output['resolution'].update(support_radius_80_m=round(r80 * 1000), support_diameter_80_m=round(r80 * 2000))
    return output
