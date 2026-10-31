#!/usr/bin/env python3
"""Rebuild the bundled water mask from public Census TIGER/Line 2025 files.

Install scripts/requirements-geo.txt in an isolated virtual environment first.
Raw source ZIPs are cached outside the repository by default. The application
needs only the generated JSON and has no geospatial Python dependencies.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen

import pyproj
import shapefile
import shapely
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from model import BAY_BOUNDS

SOURCE_BASE = "https://www2.census.gov/geo/tiger/TIGER2025/AREAWATER/"
DOCUMENTATION_URL = (
    "https://www2.census.gov/geo/pdfs/maps-data/data/tiger/tgrshp2025/"
    "TGRSHP2025_TechDoc.pdf"
)
# Bay Area counties plus neighbors needed to cover the rectangular map extent.
COUNTIES = {
    "06001": "Alameda", "06011": "Colusa", "06013": "Contra Costa",
    "06033": "Lake", "06041": "Marin", "06045": "Mendocino",
    "06047": "Merced", "06053": "Monterey", "06055": "Napa",
    "06067": "Sacramento", "06069": "San Benito", "06075": "San Francisco",
    "06077": "San Joaquin", "06081": "San Mateo", "06085": "Santa Clara",
    "06087": "Santa Cruz", "06095": "Solano", "06097": "Sonoma",
    "06099": "Stanislaus", "06113": "Yolo",
}


def polygon_parts(geometry):
    """Discard non-area fragments left by a clip or validity repair."""
    if geometry.geom_type == "Polygon":
        if not geometry.is_empty:
            yield geometry
    elif hasattr(geometry, "geoms"):
        for child in geometry.geoms:
            yield from polygon_parts(child)


def fetch_source(fips, cache):
    name = f"tl_2025_{fips}_areawater.zip"
    path = cache / name
    if not path.exists():
        with urlopen(SOURCE_BASE + name, timeout=60) as response:
            data = response.read()
        temporary = path.with_suffix(".zip.part")
        temporary.write_bytes(data)
        temporary.replace(path)
    data = path.read_bytes()
    return path, {
        "countyFips": fips, "county": COUNTIES[fips], "file": name,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def build_mask(cache, simplify_meters=5.0, padding_degrees=0.04):
    bounds = [round(BAY_BOUNDS["west"] - padding_degrees, 6),
              round(BAY_BOUNDS["south"] - padding_degrees, 6),
              round(BAY_BOUNDS["east"] + padding_degrees, 6),
              round(BAY_BOUNDS["north"] + padding_degrees, 6)]
    extent = box(*bounds)
    cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = list(pool.map(lambda fips: fetch_source(fips, cache), COUNTIES))
    geometries = []
    input_count = 0
    repaired_count = 0
    for path, metadata in sources:
        included_count = 0
        with shapefile.Reader(str(path)) as reader:
            for feature in reader.iterShapes(bbox=bounds):
                geometry = shape(feature.__geo_interface__)
                if not geometry.is_valid:
                    geometry = shapely.make_valid(geometry)
                    repaired_count += 1
                clipped = geometry.intersection(extent)
                parts = list(polygon_parts(clipped))
                if parts:
                    included_count += 1
                    geometries.extend(parts)
        metadata["includedFeatures"] = included_count
        input_count += included_count

    # Dissolve county splits before simplifying. This avoids hairline seams
    # between touching water bodies and retains land/island holes.
    merged = unary_union(geometries)
    to_meters = pyproj.Transformer.from_crs(4269, 26910, always_xy=True)
    to_wgs84 = pyproj.Transformer.from_crs(26910, 4326, always_xy=True)
    projected = transform(to_meters.transform, merged)
    simplified = projected.simplify(simplify_meters, preserve_topology=True)
    geographic = transform(to_wgs84.transform, simplified).intersection(extent)
    # A precision grid keeps rounded coordinates valid, including narrow rings.
    geographic = shapely.set_precision(geographic, 0.000001, mode="valid_output")
    parts = sorted(polygon_parts(geographic), key=lambda poly: (-poly.area, poly.bounds))
    polygons = []
    for part in parts:
        rings = [part.exterior, *part.interiors]
        polygons.append({
            "bounds": [round(value, 6) for value in part.bounds],
            "rings": [[[round(x, 6), round(y, 6)] for x, y in ring.coords]
                      for ring in rings],
        })
    result = {
        "version": 1,
        "bounds": bounds,
        "polygons": polygons,
        "source": {
            "title": "U.S. Census Bureau, 2025 TIGER/Line® Area Hydrography",
            "url": SOURCE_BASE,
            "documentationUrl": DOCUMENTATION_URL,
            "license": "Public domain: U.S. Government work (17 USC 105)",
            "vintage": 2025,
            "coordinateSystem": "EPSG:4326",
            "coordinateOrder": "longitude,latitude",
            "simplificationMeters": simplify_meters,
            "coordinatePrecisionDegrees": 0.000001,
            "boundsPaddingDegrees": padding_degrees,
            "processing": "Dissolved, simplified in NAD83 / UTM zone 10N with topology preserved, transformed to WGS84, clipped, and precision snapped.",
            "files": [metadata for _, metadata in sources],
            "limitations": [
                "Static 2025 statistical hydrography, including intermittent water; not a survey or current tide boundary.",
                "County area-water geometry does not cover the entire Pacific Ocean; the coastal zone extends beyond the application's 3 km interpolation radius.",
                "Single-line streams are absent from area hydrography.",
            ],
        },
    }
    stats = {
        "sourceFeatures": input_count,
        "repairedSourceFeatures": repaired_count,
        "polygons": len(parts),
        "holes": sum(len(part.interiors) for part in parts),
        "vertices": sum(len(ring) for item in polygons for ring in item["rings"]),
        "valid": geographic.is_valid,
        "simplificationAreaChangePercent": round(
            (simplified.area / projected.area - 1) * 100, 6),
        "originalHoles": sum(len(part.interiors) for part in polygon_parts(merged)),
        "simplifiedHoles": sum(len(part.interiors) for part in polygon_parts(simplified)),
        "sourceBytes": sum(path.stat().st_size for path, _ in sources),
    }
    if not stats["valid"] or not polygons:
        raise ValueError("Water mask must be a nonempty valid polygon geometry")
    return result, stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(tempfile.gettempdir()) / "bay-atlas-water-sources")
    parser.add_argument("--output", type=Path, default=ROOT / "public/geo/bay-water.json")
    parser.add_argument("--simplify-meters", type=float, default=5.0)
    args = parser.parse_args(argv)
    if not 0 < args.simplify_meters <= 10:
        parser.error("--simplify-meters must be greater than zero and at most 10")
    result, stats = build_mask(args.cache_dir, args.simplify_meters)
    serialized = json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized, encoding="utf-8")
    stats["outputBytes"] = len(serialized.encode("utf-8"))
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
