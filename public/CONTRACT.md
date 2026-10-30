# Bay Atlas frontend contract

The frontend requests same-origin APIs. A clean checkout serves an empty state; it never generates demonstration sales. The payload examples below describe the schema and are not market observations.

## Data and model

`GET /api/data` returns a single `sales` array, `metadata`, `model`, `coverage`, `status`, and validation diagnostics. Each row contains a provider identity, address, city, coordinates, actual sale price, interior square feet, bedroom count, ISO sale date, an allowlisted HTTPS source URL, and `time_weight`. Metadata supplies the timestamp, source, record count, coverage limitations, and status.

```json
{
  "sales": [],
  "status": "awaiting_data",
  "metadata": {
    "status": "awaiting_data",
    "record_count": 0,
    "message": "Dated sold-home records have not been imported yet."
  }
}
```

The frontend also accepts `rows` or legacy `properties` arrays, but the HTTP service avoids duplicating records on the wire. Rows are revalidated against the current UTC date, including the two-calendar-year limit. Internal snapshot aliases may differ from the serialized response. The model includes `as_of`, `oldest_sale_date`, and time-bucket metadata; the worker uses each supplied row's `time_weight` for consistency with the backend.

`GET /api/estimate?lat=...&lng=...&sqft=2500&beds=3` returns `estimate_ppsf`, `estimated_price`, `range_ppsf:{low,high}`, `status`, `comparable_count`, `effective_sample_count`, `radius_km`, and `comparables`. Individual comparables include the components of their model weight. Insufficient support produces a null estimate with an explanatory status, never a fabricated value. Errors use JSON `{ "error": "..." }` with a corresponding HTTP status.

The surface represents a continuous robust weighted geometric mean of nearby sold prices per square foot. It uses the same adaptive 0.75/1.5/2.25/3 km radius, Gaussian distance and size weights, bedroom similarity, and weighted log-price clipping as the backend. Areas below three distinct comps or two effective comps remain blank; transparency indicates limited support. Browser interpolation smooths the sampled scalar field. It is not a sale-density heat map.

The default subject is 2,500 square feet and three bedrooms. The selectors support 2,000–3,000 square feet and 2, 3, 4, or 5+ bedrooms. Bedroom weighting changes similarity to observed comps rather than applying a fixed price premium. The displayed range describes the 20th–80th percentile of comparable prices, not a prediction interval. See [the model specification](../README-model.md).

## Address search

`GET /api/suggest?q=...` returns `{results:[{lat,lng,display_name,source,precision}],remote_status,...}`. Queries must contain 6–120 characters. Saved sales are searched locally, and partial text may be sent to Photon. External suggestions are debounced, paced, bounded, cached, and restricted to the Bay Area. Approximate street or place locations are distinguished from mapped addresses.

`GET /api/geocode?q=...` uses a submitted full address and returns `{results:[{lat,lng,display_name,source,precision}],cached,provider}`. It uses the U.S. Census Geocoder; coordinates may be interpolated. Empty results are an ordinary no-match outcome. The frontend accepts a legacy flat `{lat,lng,display_name}` response too.

Search results are inserted as text, not trusted HTML. Selecting a suggestion requests a backend estimate. Loading state and errors must remain visible and must not be confused with successful data retrieval.

## Import, refresh, and connection

**Update data** opens the data dialog. `POST /api/import/redfin` accepts same-origin JSON `{csv,filename}` for one supported export, bounded to 5 MiB. Successful imports return counts, provenance, warnings, and partial/manual-source status. A manual CSV import does not establish an automated Redfin connection. Reload the data after successful publication.

`GET /api/refresh` reads refresh state and progress. `POST /api/refresh` starts or resumes an explicitly requested provider refresh; the frontend polls while it is running. Periodic local-data reloads do not initiate provider jobs.

`GET /api/provider` returns sanitized connection status and budget controls. `POST /api/provider` accepts supported provider settings and an optional replacement token. The key input uses a password field, is cleared after use or closing, and is not stored in browser storage. Provider credentials must never appear in API responses or frontend assets.

Apify free-mode controls include verified-free-plan confirmation and local credit/run limits. Paid allowance provisioning is an operator-only private configuration step; the browser cannot create, enlarge, or extend it. The HouseData adapter has per-refresh and rolling 31-day request limits. See [provider operations](../PROVIDERS.md).

All mutations require JSON and a matching same-origin `Origin` header. Host allowlisting is enforced separately. Neither check replaces user authentication for a remote deployment.

## Assets and external services

Leaflet, DM Sans, and Manrope are vendored under `public/vendor/` with their license notices. Browser map tiles come from OpenStreetMap. The map estimator runs in a cancellable Web Worker so panning and controls can remain responsive. APIs, static assets, and worker URLs are relative to support both the root path and `/homes/`.
