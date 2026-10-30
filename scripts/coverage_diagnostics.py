#!/usr/bin/env python3
"""Summarize saved sale coverage without network requests or property details.

Use --root for the application directory, --as-of for a reproducible eligibility
date, and repeat --city to include a municipality even when it has zero sales.
This reports returned observations, not an inventory of all market sales.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from model import prepare_sales, today_utc


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _place(value):
    # Only municipality/county labels enter the aggregate report. Do not echo
    # arbitrary provider status text, URLs, run identifiers or street addresses.
    if not isinstance(value, str) or not value or len(value) > 120:
        return "Unknown"
    if not all(char.isalpha() or char in " .,'’-" for char in value):
        return "Unknown"
    return " ".join(value.split()) or "Unknown"


def summarize(snapshot, *, as_of=None, cities=()):
    as_of = as_of or today_utc()
    rows = snapshot.get("sales", snapshot.get("rows", []))
    prepared, diagnostics = prepare_sales(rows, as_of=as_of)
    city_counts = Counter(_place(row.get("city")) for row in prepared)
    for city in cities:
        city_counts.setdefault(_place(city), 0)
    source_counts = Counter(row["source"] for row in prepared)
    coverage = snapshot.get("coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    gaps = []
    recorded_gaps = coverage.get("verified_source_gaps")
    for gap in recorded_gaps if isinstance(recorded_gaps, list) else []:
        if not isinstance(gap, dict):
            continue
        count = _count(gap.get("unreachable_count"))
        gaps.append({"county": _place(gap.get("county")),
                     "unreachable_count": count,
                     "source_coverage": "partial" if gap.get("source_coverage") == "partial" else "unknown"})
    warnings = []
    for gap in gaps:
        count = gap["unreachable_count"]
        if count:
            warnings.append(f"{gap['county']}: the provider reported {count:,} unreachable listings.")
        elif gap["source_coverage"] == "partial":
            warnings.append(f"{gap['county']}: a saved provider report identifies partial coverage.")
    if coverage.get("complete") is True:
        warnings.append("The snapshot claims complete coverage; this local report cannot verify that claim.")
    return {
        "as_of": as_of.isoformat(), "read_only": True, "external_requests": 0,
        "published_row_count": len(rows), "current_eligible_count": len(prepared),
        "eligibility": diagnostics,
        "coverage": {
            "reported_complete": coverage.get("complete") if type(coverage.get("complete")) is bool else None,
            "counties_completed": _count(coverage.get("counties_completed")),
            "counties_total": _count(coverage.get("counties_total")),
            "county_searches_finished": coverage.get("county_searches_finished") is True,
            "verified_source_gaps": gaps,
        },
        "city_counts": dict(sorted(city_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "warnings": warnings,
        "limitations": [
            "Finished county searches do not establish complete sale coverage.",
            "City counts use saved source labels, not verified administrative boundaries.",
            "Zero returned sales does not establish zero sales in the market.",
            "Provider unreachable counts do not establish how many missing records meet every eligibility rule.",
            "No saved gap report does not establish that a source has no gaps.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--city", action="append", default=[])
    args = parser.parse_args(argv)
    path = args.root / "data" / "sales.json"
    original = path.read_bytes()
    report = summarize(json.loads(original), as_of=args.as_of, cities=args.city)
    report["dataset_sha256"] = hashlib.sha256(original).hexdigest()
    report["coherent_reads"] = path.read_bytes() == original
    if not report["coherent_reads"]:
        report["warnings"].append("The saved dataset changed during this report; rerun for a coherent snapshot.")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["coherent_reads"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
