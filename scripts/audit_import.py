#!/usr/bin/env python3
"""Read-only local audit of a completed Bay Atlas county import; no API calls.

Run from any directory. JSON goes to stdout. Exit 1 means a data/coherence
failure, 2 means the current generation is not yet fully published. County
counts describe source searches, not polygon-verified county membership.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--allow-in-progress', action='store_true', help='Do not exit 2 before publication')
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    from model import (canonical_address, estimate, haversine_km, parse_date,
                       prepare_sales, source_identity, today_utc, two_year_cutoff)

    as_of = args.as_of or today_utc()
    paths = [root / 'data/sales.json', root / 'data/apify-state.json']
    originals = [p.read_bytes() for p in paths]
    data, state = [json.loads(blob) for blob in originals]
    rows = data.get('sales', data.get('rows', []))
    generation = state.get('generation') or {}
    generation_id = generation.get('id', '')
    if not re.fullmatch(r'[a-f0-9]{32}', generation_id):
        raise ValueError('Missing or unsafe generation identifier')
    jobs = generation.get('jobs', [])
    prepared, diagnostics = prepare_sales(rows, as_of=as_of)
    identities = Counter(source_identity(r) for r in rows)
    addresses = Counter(canonical_address(r) for r in rows)
    published_ids = set(identities)
    excluded_ids = {source_identity(r) for r in data.get('excluded_attached_properties', [])}
    failures, warnings, county_audits, estimates = [], [], [], []

    def price_class(row):
        evidence = str(row.get('price_evidence', ''))
        if 'may be rounded' in evidence:
            return 'possibly_rounded_display_price'
        if 'exact' in evidence or evidence.startswith('Redfin CSV PRICE') or 'paired lastSoldDate' in evidence:
            return 'exact_sold_price_evidence'
        return 'other_or_unspecified'

    def counts(values):
        return dict(sorted(Counter(values).items()))

    for index, job in enumerate(jobs):
        county = job.get('county')
        item = {key: job.get(key) for key in (
            'county', 'state', 'input_count', 'eligible_count', 'result_limit_reached',
            'provider_cap_reached', 'dataset_complete', 'http_calls', 'read_bytes')}
        stage_path = root / 'data/apify-cache' / generation_id / f'{index}.json'
        staged_rows = []
        if job.get('state') == 'succeeded':
            if not stage_path.exists():
                failures.append(f'Missing saved county dataset: {county}')
            else:
                staged = json.loads(stage_path.read_bytes())
                staged_rows = staged.get('sales', [])
                valid, stage_checks = prepare_sales(staged_rows, as_of=as_of)
                stage_ids = {source_identity(r) for r in valid}
                item.update(
                    staged_rows=len(staged_rows), staged_unique_source_identities=len(stage_ids),
                    surviving_published_source_identities=len(stage_ids & published_ids),
                    stage_validation=stage_checks,
                    normalization_diagnostics=staged.get('diagnostics'),
                    source_counts=counts(r.get('source', 'unknown') for r in valid),
                    price_evidence_classes=counts(price_class(r) for r in valid),
                    date_min=min((r['sold_date'] for r in valid), default=None),
                    date_max=max((r['sold_date'] for r in valid), default=None),
                    city_counts=counts(r.get('city', '') for r in valid),
                )
                if staged.get('county') != county or staged.get('run_id') != job.get('run_id'):
                    failures.append(f'County checkpoint identity mismatch: {county}')
                if job.get('eligible_count') != len(staged_rows):
                    failures.append(f'County eligible count differs from saved rows: {county}')
                if job.get('provider_cap_reached') or job.get('result_limit_reached'):
                    warnings.append(f'Source/result cap reached: {county}')
                if valid:
                    # Reproducible spatial smoke test, not an accuracy backtest.
                    # Choose a saved point nearest the county-search median.
                    mid_lat = statistics.median(r['lat'] for r in valid)
                    mid_lng = statistics.median(r['lng'] for r in valid)
                    anchor = min(valid, key=lambda r: (haversine_km(mid_lat, mid_lng, r['lat'], r['lng']), source_identity(r)))
                    result = estimate(prepared, anchor['lat'], anchor['lng'], 2500, 3, as_of=as_of, prepared=True)
                    estimates.append(dict(county=county, sample_city=anchor['city'],
                        sample_lat=anchor['lat'], sample_lng=anchor['lng'], sqft=2500, beds=3,
                        **{key: result.get(key) for key in ('status', 'estimate_ppsf', 'estimated_price',
                            'range_ppsf', 'comparable_count', 'effective_sample_count', 'radius_km')}))
                    if result['estimate_ppsf'] is None:
                        warnings.append(f'Insufficient nearby comps at sample point: {county}')
        if not staged_rows:
            estimates.append(dict(county=county, status='no_completed_county_sample'))
        county_audits.append(item)

    duplicate_ids = {key: value for key, value in identities.items() if value > 1}
    duplicate_addresses = {key: value for key, value in addresses.items() if value > 1}
    published_attached = sorted(published_ids & excluded_ids)
    if diagnostics['excluded']:
        failures.append('Published saved rows fail current eligibility or deduplication checks')
    if duplicate_ids or duplicate_addresses:
        failures.append('Duplicate source identities or canonical addresses in published rows')
    if published_attached:
        failures.append('Explicitly excluded attached properties remain published')
    complete_runs = {job.get('run_id') for job in jobs if job.get('state') == 'succeeded'}
    last = data.get('last_api_refresh') or {}
    expected_counties = 9
    terminal = len(jobs) == expected_counties and all(job.get('state') in {'succeeded', 'failed'} for job in jobs)
    published = bool(terminal and last.get('generation') == generation_id
                     and complete_runs == set(last.get('published_run_ids', [])))
    successful = sum(job.get('state') == 'succeeded' for job in jobs)
    if successful < expected_counties:
        warnings.append(f'Only {successful}/9 county searches have succeeded')
    coherent = all(p.read_bytes() == blob for p, blob in zip(paths, originals))
    if not coherent:
        failures.append('Dataset or job state changed during audit; rerun after publication')
    coverage = data.get('coverage', {})
    if coverage.get('complete') is True:
        warnings.append('Coverage is labeled complete, which successful searches alone cannot establish')
    age_buckets = Counter()
    for row in prepared:
        age = (as_of - parse_date(row['sold_date'])).days
        label = next((label for ceiling, label in ((30, '0-30d'), (90, '31-90d'),
            (183, '91-183d'), (365, '184-365d')) if age <= ceiling), '366d-2_calendar_years')
        age_buckets[label] += 1
    report = {
        'audit_generated_at': datetime.now(timezone.utc).isoformat(), 'as_of': as_of.isoformat(),
        'root': str(root), 'read_only': True, 'external_requests': 0,
        'dataset_sha256': hashlib.sha256(originals[0]).hexdigest(),
        'state_sha256': hashlib.sha256(originals[1]).hexdigest(), 'coherent_reads': coherent,
        'generation_id': generation_id, 'generation_as_of': generation.get('as_of'),
        'all_jobs_terminal': terminal, 'current_generation_fully_published': published,
        'successful_counties': successful, 'expected_counties': expected_counties,
        'data_checks_pass': not failures, 'failures': failures, 'warnings': warnings,
        'published_row_count': len(rows), 'current_eligible_count': len(prepared),
        'eligibility': diagnostics, 'oldest_allowed_date': two_year_cutoff(as_of).isoformat(),
        'sold_date_min': min((r['sold_date'] for r in prepared), default=None),
        'sold_date_max': max((r['sold_date'] for r in prepared), default=None),
        'recency_bucket_counts': dict(age_buckets),
        'source_counts': counts(r.get('source', 'unknown') for r in prepared),
        'price_evidence_counts': counts(r.get('price_evidence', 'unspecified') for r in prepared),
        'price_evidence_classes': counts(price_class(r) for r in prepared),
        'detachment_evidence_counts': counts(r.get('detachment_evidence', 'unspecified') for r in prepared),
        'explicit_attached_exclusions': len(excluded_ids), 'excluded_attached_still_published': published_attached,
        'duplicate_source_identities': duplicate_ids, 'duplicate_canonical_addresses': duplicate_addresses,
        'coverage': coverage, 'county_search_audits': county_audits, 'sample_estimates': estimates,
        'limitations': [
            'County attribution is the source search/checkpoint, not verified administrative boundaries.',
            'The area filter is a Bay Area bounding box, not county polygons.',
            'Single-family classification without an attached flag is weaker than affirmative detached evidence.',
            'Sample estimates use county-search median-area saved coordinates and include nearby saved homes; they are smoke tests, not prediction-accuracy tests.',
            'Successful searches and complete downloaded datasets do not establish exhaustive market coverage.',
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if failures else (2 if not published and not args.allow_in_progress else 0)


if __name__ == '__main__':
    raise SystemExit(main())
