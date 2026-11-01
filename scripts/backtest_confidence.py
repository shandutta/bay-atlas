#!/usr/bin/env python3
"""Retrospective, strictly earlier-sale validation; emits aggregate statistics only.

Example: python3 scripts/backtest_confidence.py --data data/sales.json \
    --start 2026-05-01 --validation-start 2026-08-01 --end 2026-10-31
No network access or provider calls. Input data and individual predictions stay private.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model
from confidence import assess_confidence, TARGET

CELL_DEGREES = .025


def geographic_cell(lat, lng, size_km=.5):
    return (math.floor(lng * 111.32 * math.cos(math.radians(37.8)) / size_km),
            math.floor(lat * 111.195 / size_km))


def prepare_history(raw_rows, start, end):
    """Validate records without dropping an older training sale at the end date.

    A raw historical file may contain multiple sales for a home. Keep those events
    here; choose the latest earlier one separately at each prediction date.
    """
    records = []
    cutoff = model.two_year_cutoff(start)
    for row in raw_rows:
        try:
            sold = model.parse_date(row.get('sold_date'))
        except (AttributeError, TypeError, ValueError):
            continue
        if not cutoff <= sold <= end:
            continue
        clean, _ = model.prepare_sales([row], as_of=sold)
        records.extend(clean)
    return records


class HistoricalIndex:
    def __init__(self, records):
        self.bins = defaultdict(list)
        for row in records:
            row = dict(row)
            row['_identity'] = model.source_identity(row)
            row['_canonical'] = model.canonical_address(row)
            row['_sold'] = model.parse_date(row['sold_date'])
            self.bins[(math.floor(row['lat'] / CELL_DEGREES), math.floor(row['lng'] / CELL_DEGREES))].append(row)

    def comparables_before(self, subject, as_of):
        """Exclude same-day/future/property leakage before radius selection."""
        cutoff = model.two_year_cutoff(as_of)
        identity, address = model.source_identity(subject), model.canonical_address(subject)
        lat, lng = subject['lat'], subject['lng']
        bx, by = math.floor(lat / CELL_DEGREES), math.floor(lng / CELL_DEGREES)
        candidates = []
        for x in range(bx - 2, bx + 3):
            for y in range(by - 2, by + 3):
                for row in self.bins.get((x, y), []):
                    if not cutoff <= row['_sold'] < as_of:
                        continue
                    # Conservative cheap bounds; exact distance remains in model.estimate.
                    if abs(row['lat'] - lat) > .0271 or abs(row['lng'] - lng) > .036:
                        continue
                    if row['_identity'] == identity or row['_canonical'] == address:
                        continue
                    if (abs(row['lat'] - lat) < .0003 and abs(row['lng'] - lng) < .0004
                            and model.same_property(row, subject)):
                        continue
                    candidates.append(row)
        # Deduplicate only after time filtering: a later resale must not erase
        # that home's earlier eligible sale from a historical prediction.
        candidates.sort(key=lambda row: row['_sold'], reverse=True)
        identities, addresses, cleaned = set(), set(), []
        for row in candidates:
            if row['_identity'] in identities or row['_canonical'] in addresses:
                continue
            identities.add(row['_identity'])
            addresses.add(row['_canonical'])
            # Records were fully validated in prepare_history; only time-specific
            # fields and latest-earlier deduplication need recomputation here.
            cleaned.append(dict(row, age_days=(as_of - row['_sold']).days,
                                time_weight=model.time_weight(row['_sold'], as_of)))
        return cleaned


def predict_subject(subject, index):
    as_of = model.parse_date(subject['sold_date'])
    candidates = index.comparables_before(subject, as_of)
    result = model.estimate(candidates, subject['lat'], subject['lng'], subject['sqft'],
                            int(subject['beds']), as_of=as_of, prepared=True)
    evidence = result.get('evidence') or assess_confidence(result, as_of)
    predicted = result['estimate_ppsf']
    return {
        'date': as_of.isoformat(), 'cell': geographic_cell(subject['lat'], subject['lng']),
        'actual': subject['price'] / subject['sqft'], 'predicted': predicted,
        'tier': evidence['evidence_tier'],
        'support_diameter_km': (evidence['resolution']['support_diameter_80_m'] / 1000
                                if evidence['resolution']['support_diameter_80_m'] is not None else None),
    }


def quantile(values, fraction):
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)] if ordered else None


def summary(rows, bootstrap=0):
    predicted = [row for row in rows if row['predicted'] is not None]
    errors = [abs(row['predicted'] / row['actual'] - 1) for row in predicted]
    cells = defaultdict(list)
    blocks = defaultdict(list)
    for row in predicted:
        cells[tuple(row['cell'])].append(math.log(row['predicted'] / row['actual']))
        blocks[(math.floor(row['cell'][0] / 4), math.floor(row['cell'][1] / 4))].append(row)
    cell_errors = [abs(math.exp(statistics.median(values)) - 1) for values in cells.values() if len(values) >= 3]
    result = {
        'held_out_homes': len(rows), 'available_predictions': len(predicted),
        'median_absolute_percentage_error': statistics.median(errors) if errors else None,
        'p80_absolute_percentage_error': quantile(errors, .8),
        'fraction_within_20_percent': sum(error <= .2 for error in errors) / len(errors) if errors else None,
        'median_support_diameter_km': statistics.median(row['support_diameter_km'] for row in predicted) if predicted else None,
        'neighborhood_cells_with_at_least_3_held_out_homes': len(cell_errors),
        'neighborhood_median_absolute_error': statistics.median(cell_errors) if cell_errors else None,
        'neighborhood_p80_absolute_error': quantile(cell_errors, .8),
        'spatial_2km_blocks': len(blocks),
    }
    if bootstrap and blocks:
        groups, randomizer, rates = list(blocks.values()), random.Random(6149), []
        for _ in range(bootstrap):
            sample = [row for group in randomizer.choices(groups, k=len(groups)) for row in group]
            rates.append(sum(abs(row['predicted'] / row['actual'] - 1) <= .2 for row in sample) / len(sample))
        result['within_20_percent_cluster_bootstrap_95_range'] = [quantile(rates, .025), quantile(rates, .975)]
    return result


def backtest(raw_rows, start, validation_start, end, bootstrap=800):
    if not start < validation_start <= end:
        raise ValueError('Require start < validation-start <= end')
    history = prepare_history(raw_rows, start, end)
    index = HistoricalIndex(history)
    targets, _ = model.prepare_sales(history, as_of=end)
    targets = [row for row in targets if start <= model.parse_date(row['sold_date']) <= end]
    results = [predict_subject(subject, index) for subject in targets]
    output = {
        'protocol_version': 1, 'start': start.isoformat(), 'validation_start': validation_start.isoformat(),
        'end': end.isoformat(), 'validated_historical_records': len(history),
        'target_thresholds': TARGET, 'periods': {},
        'limitations': [
            'Retrospective observed data, not a historical archive of which records were available at each date.',
            'Subjects are the latest observed sale per property; source selection and missing or overwritten older sales can bias results.',
            'Neighborhood cell residuals are descriptive and do not establish a calibrated confidence interval.',
            'Spatial dependence can extend beyond the 2 km bootstrap blocks.',
        ],
    }
    for name, later in [('development', False), ('validation', True)]:
        subset = [row for row in results if (model.parse_date(row['date']) >= validation_start) == later]
        output['periods'][name] = {'all': summary(subset, bootstrap)}
        for tier in ['target', 'usable', 'limited', 'insufficient']:
            output['periods'][name][tier] = summary([row for row in subset if row['tier'] == tier], bootstrap)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--start', type=date.fromisoformat, required=True)
    parser.add_argument('--validation-start', type=date.fromisoformat, required=True)
    parser.add_argument('--end', type=date.fromisoformat, required=True)
    parser.add_argument('--bootstrap', type=int, default=800)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 0 <= args.bootstrap <= 10000:
        parser.error('--bootstrap must be between 0 and 10000')
    data_bytes = args.data.read_bytes()
    payload = json.loads(data_bytes)
    raw_rows = payload['sales'] if isinstance(payload, dict) else payload
    output = backtest(raw_rows, args.start, args.validation_start, args.end, args.bootstrap)
    output.update(input_sha256=hashlib.sha256(data_bytes).hexdigest(),
                  generated_at=datetime.now(timezone.utc).isoformat())
    rendered = json.dumps(output, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        print(rendered, end='')


if __name__ == '__main__':
    main()
