#!/usr/bin/env python3
"""Offline coverage audit on occupied residential-evidence cells.

This deliberately does not call every empty land cell a data gap. Its denominator
is cells containing eligible observed sales, so wholly unobserved neighborhoods
remain unknown. Outputs contain private sale-derived locations; keep them outside
the public source tree. No network calls, imports, or provider mutations occur.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model import canonical_address, estimate, prepare_sales, same_property, source_identity, today_utc
from confidence import VERSION as CONFIDENCE_VERSION, TARGET, assess_confidence

KM_PER_LAT_DEGREE = 111.1950802335329
REFERENCE_LATITUDE = 37.8
X_SCALE = KM_PER_LAT_DEGREE * math.cos(math.radians(REFERENCE_LATITUDE))


def cell_key(lat, lng, grid_m=500):
    """Locally metric grid, scale error below 2% across the model's latitude span."""
    if not math.isfinite(grid_m) or grid_m <= 0:
        raise ValueError("Grid spacing must be positive")
    width = grid_m / 1000
    return (math.floor(lng * X_SCALE / width),
            math.floor(lat * KM_PER_LAT_DEGREE / width))


def cell_center(key, grid_m=500):
    width = grid_m / 1000
    return ((key[1] + .5) * width / KM_PER_LAT_DEGREE,
            (key[0] + .5) * width / X_SCALE)


def postal_code(row):
    match = re.search(r"\bCA\s*,?\s*(\d{5})\b", row.get("address", ""), re.I)
    return match[1] if match else "unknown"


def canonical_city(value):
    compact = re.sub(r"[^a-z]", "", value.casefold())
    aliases = {"sanjose": "San Jose", "sanmateo": "San Mateo", "sanramon": "San Ramon",
               "sanrafael": "San Rafael", "losgatos": "Los Gatos", "losgatos1": "Los Gatos",
               "halfmoonbay": "Half Moon Bay", "mountainhouse": "Mountain House",
               "ptreyesstation": "Point Reyes Station"}
    return aliases.get(compact, value.title())


def summarize(cells):
    states = Counter(row["status"] for row in cells)
    reasons = Counter(reason for row in cells
                      for reason in row.get("failed_gates", row.get("reasons", [])))
    tiers = Counter(row["evidence_tier"] for row in cells if "evidence_tier" in row)
    assessed = any("target_met" in row for row in cells)
    targets = sum(row.get("target_met", False) for row in cells) if assessed else None
    return {
        "occupied_cells": len(cells), "eligible_homes": sum(row["homes"] for row in cells),
        "current_status_counts": dict(states), "target_met_cells": targets,
        "target_met_fraction": round(targets / len(cells), 4) if cells and assessed else None,
        "failed_gate_counts": dict(reasons),
        "evidence_tier_counts": dict(tiers),
        "median_effective_homes": percentile([row["effective_homes"] for row in cells], .5),
        "median_support_radius_km": percentile([row["radius_km"] for row in cells], .5),
    }


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return round(values[int((len(values) - 1) * fraction)], 3)


def confidence_profile(result):
    assessment = result.get("evidence") or assess_confidence(result)
    return {key: assessment[key] for key in
            ("evidence_tier", "target_met", "metrics", "resolution", "reasons")}


def audit(sales, as_of=None, grid_m=500, assessor=None, estimator=None, reference_cells=None):
    """Assess one held-out home per occupied cell; optional assessor takes estimate.

    Excluding each representative home prevents it validating its own coverage.
    Choosing an actual observed location avoids making residential assertions
    about a cell center that happens to fall in water, a park, or farmland.
    Pass a prior report's cells as reference_cells to freeze the probe locations
    in overlapping cells before and after data acquisition. New cells still get
    a new observed-home probe. Store these private outputs outside the repo.
    """
    as_of = as_of or today_utc()
    estimator = estimator or estimate
    assessor = assessor or confidence_profile
    cell_key(0, 0, grid_m)  # Validate spacing even when there are no sales.
    references = {tuple(row["cell"]): row for row in (reference_cells or [])}
    sales, diagnostics = prepare_sales(sales, as_of)
    groups = defaultdict(list)
    property_indexes = ({}, {}, {})
    def property_keys(row):
        return source_identity(row), canonical_address(row), canonical_address(row, omit_suffix=True)
    for row in sales:
        groups[cell_key(row["lat"], row["lng"], grid_m)].append(row)
        for lookup, identity in zip(property_indexes, property_keys(row)):
            lookup.setdefault(identity, []).append(row)
    cells = []
    # Candidate lookup limits work without changing the estimator's final
    # spherical 3 km inclusion rule. Extra cells cover local grid scale error.
    lookup_span = math.ceil(3.1 / (grid_m / 1000)) + 1
    for key, homes in sorted(groups.items()):
        center_lat, center_lng = cell_center(key, grid_m)
        representative = min(homes, key=lambda row: (
            ((row["lat"] - center_lat) * KM_PER_LAT_DEGREE) ** 2
            + ((row["lng"] - center_lng) * X_SCALE) ** 2, row["id"]))
        reference = references.get(key)
        probe_lat = reference["lat"] if reference else representative["lat"]
        probe_lng = reference["lng"] if reference else representative["lng"]
        probe_id = reference["probe_id"] if reference else representative["id"]
        probe_property = reference.get("probe_property") if reference else {
            name: representative[name] for name in
            ("id", "address", "city", "lat", "lng", "source_url", "source_id", "street_address")
            if name in representative}
        if not probe_property:
            raise ValueError("Reference probes must retain property identity to prevent cross-provider holdout leakage")
        possible_matches = {id(row): row for lookup, identity in zip(property_indexes, property_keys(probe_property))
                            for row in lookup.get(identity, [])}
        held_out = {identity for identity, row in possible_matches.items() if same_property(row, probe_property)}
        neighbors = [row for x in range(key[0] - lookup_span, key[0] + lookup_span + 1)
                     for y in range(key[1] - lookup_span, key[1] + lookup_span + 1)
                     for row in groups.get((x, y), []) if id(row) not in held_out]
        result = estimator(neighbors, probe_lat, probe_lng,
                           as_of=as_of, prepared=True)
        profile = {
            "cell": list(key), "lat": probe_lat, "lng": probe_lng, "probe_id": probe_id,
            "probe_property": probe_property,
            "city": reference["city"] if reference else canonical_city(representative["city"]),
            "zip": reference["zip"] if reference else postal_code(representative),
            "homes": len(homes), "recent_homes_183d": sum(row["age_days"] <= 183 for row in homes),
            "source_counts": dict(Counter(row["source"] for row in homes)),
            "status": result["status"], "effective_homes": result["effective_sample_count"],
            "comparables": result["comparable_count"], "radius_km": result["radius_km"],
            "estimate_ppsf": result["estimate_ppsf"],
        }
        if assessor:
            profile.update(assessor(result))
        cells.append(profile)
    by_city, by_zip = defaultdict(list), defaultdict(list)
    for row in cells:
        by_city[row["city"]].append(row)
        by_zip[row["zip"]].append(row)
    current_keys = {tuple(row["cell"]) for row in cells}
    reused_cells = [row for row in cells if tuple(row["cell"]) in references]
    comparison = None
    if reference_cells is not None:
        comparison = {
            "prior_cells": len(references), "retained_cells": len(reused_cells),
            "new_cells": len(current_keys - references.keys()),
            "absent_prior_cells": len(references.keys() - current_keys),
            "before_same_cells": summarize([row for key, row in references.items() if key in current_keys]),
            "after_same_cells": summarize(reused_cells),
            "new_cell_summary": summarize([row for row in cells if tuple(row["cell"]) not in references]),
        }
    return {
        "as_of": as_of.isoformat(), "grid_m": grid_m,
        "confidence_version": CONFIDENCE_VERSION, "confidence_target": dict(TARGET),
        "confidence_interpretation": "Evidence-quality acquisition target; not a calibrated probability or prediction interval",
        "grid_projection": "Local equirectangular at 37.8 degrees latitude; scale error below 2%",
        "denominator": "Cells with at least one eligible observed sale; unobserved neighborhoods unknown",
        "probe": "Closest observed home to cell center, excluded from its own comparables",
        "area_labels": "Cells belong to the representative home's city and ZIP; cells can cross boundaries",
        "default_subject": {"sqft": 2500, "bedrooms": 3},
        "reference_cells_reused": sum(tuple(row["cell"]) in references for row in cells),
        "reference_comparison": comparison,
        "diagnostics": diagnostics, "summary": summarize(cells),
        "cities": {city: summarize(rows) for city, rows in sorted(by_city.items())},
        "zips": {postal: summarize(rows) for postal, rows in sorted(by_zip.items())},
        "cells": cells,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=today_utc())
    parser.add_argument("--grid-m", type=float, default=500)
    parser.add_argument("--reference-audit", type=Path,
                        help="Reuse prior probe locations in overlapping cells")
    args = parser.parse_args()
    payload = args.data.read_bytes()
    document = json.loads(payload)
    reference = json.loads(args.reference_audit.read_text()) if args.reference_audit else None
    if reference and reference["grid_m"] != args.grid_m:
        raise ValueError("Reference audit must use the same grid spacing")
    report = audit(document["sales"] if isinstance(document, dict) else document,
                   args.as_of, args.grid_m, reference_cells=reference["cells"] if reference else None)
    report["input_sha256"] = hashlib.sha256(payload).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    args.output.chmod(0o600)
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
