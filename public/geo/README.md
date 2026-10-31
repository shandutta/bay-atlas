# Bay Area water mask

`bay-water.json` is a bundled, public water mask for clipping the continuous
price surface at shorelines. It contains no property records. Browsers load it
from this application; no external geometry service is needed at runtime.

## Source and attribution

Source: **U.S. Census Bureau, 2025 TIGER/Line® Area Hydrography**. The original
county ZIP files are published in the
[Census AREAWATER archive](https://www2.census.gov/geo/tiger/TIGER2025/AREAWATER/).
The [technical documentation](https://www2.census.gov/geo/pdfs/maps-data/data/tiger/tgrshp2025/TGRSHP2025_TechDoc.pdf)
describes area hydrography in section 4.10 and reuse/citation in sections 1.1–1.2.
These U.S. Government data are in the public domain under 17 USC 105. TIGER/Line®
is a registered trademark of the U.S. Census Bureau; this application is an
independent derivative and is not a Census Bureau product or endorsement.

The asset records each original filename, county FIPS code, and SHA-256 digest.
Its 20 county sources cover the nine Bay Area counties and adjoining portions
of the map rectangle: Alameda, Colusa, Contra Costa, Lake, Marin, Mendocino,
Merced, Monterey, Napa, Sacramento, San Benito, San Francisco, San Joaquin,
San Mateo, Santa Clara, Santa Cruz, Solano, Sonoma, Stanislaus, and Yolo.

## Geometry and format

The mask bounds are `[-123.64, 36.76, -121.11, 38.94]` in
`[west, south, east, north]` order. They pad the model's Bay Area bounds by 0.04°,
more than the model's maximum 3 km interpolation radius at these latitudes.

County water polygons are clipped and dissolved before a topology-preserving
5 m simplification in NAD83 / UTM zone 10N. They are transformed to WGS84 and
snapped to a 0.000001° precision grid. The build retains all 611 land holes;
simplification changes total water area by −0.015007%. The generated geometry
is valid and has 4,290 polygons and 142,906 vertices (including ring closure
coordinates). Source ZIPs total about 6.7 MB; the JSON is about 3.7 MB before
HTTP compression.

```json
{
  "version": 1,
  "bounds": [-123.64, 36.76, -121.11, 38.94],
  "polygons": [
    {
      "bounds": ["west", "south", "east", "north"],
      "rings": ["exterior water ring", "optional land/island holes"]
    }
  ],
  "source": {"title": "source attribution and processing metadata"}
}
```

The illustrative strings above stand for numeric bounds and arrays of closed
rings. Each ring coordinate is `[longitude, latitude]`. The first ring is the
water polygon exterior; later rings are land holes and must remain unmasked.
Use even-odd filling **per polygon**, and use the polygon bounds to skip polygons
outside the viewport. The mask is dissolved so county boundaries do not create
antialiased seams.

This is a static statistical map, not a survey or a current tide measurement.
The source includes intermittent water and some marshes. Narrow streams stored
only as lines are absent. Marine polygons cover county waters rather than the
entire Pacific Ocean; the coastal coverage extends beyond the model's 3 km
interpolation radius. Very close shoreline alignment can differ from a basemap
because the source vintages and survey detail differ.

## Rebuild

Use an isolated environment; these packages are only needed to regenerate the
asset, not to run the application:

```sh
python3 -m venv /tmp/bay-atlas-geo-venv
/tmp/bay-atlas-geo-venv/bin/python -m pip install -r scripts/requirements-geo.txt
/tmp/bay-atlas-geo-venv/bin/python scripts/build_water_mask.py
```

The default cache is `bay-atlas-water-sources` beneath the operating system's
temporary directory, outside the repository. `--cache-dir` can select another
location. Keep raw ZIPs and rebuild environments out of public commits. The
builder reports validity, feature counts, hole counts, area change, and output
size. Compare the source digests in the generated asset when reproducing or
updating it. The builder and data use only public geographic sources.
