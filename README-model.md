# Bay Atlas backend and model

Run `python3 server.py --port 8788`. The service binds `127.0.0.1` by default. Remote access requires an authenticated reverse proxy configured by the operator; the app has no built-in login. Python 3.9+ is sufficient. Only `public/` is served as files. Run checks with `python3 -m unittest discover -s tests -v`.

## Data

`data/sales.json` contains `{updated_at, source, sources, coverage, sales: [...]}`. Each sale requires `id`, `address`, `city`, `lat`, `lng`, actual closed-sale `price`, `sqft`, `beds`, ISO `sold_date`, `source_url` and `home_type: "singleFamily"`. Sources are Zillow and supported Redfin sold-home CSV exports. Asking prices and Zestimates never substitute for closed prices; missing sale dates are not inferred. Import history preserves file hashes, times, eligibility counts, and merge outcomes.

The backend excludes missing/invalid fields, source links outside the HTTPS Zillow/Redfin allowlist, non-single-family types, explicitly attached homes, non-sold statuses, homes outside the Bay Area bounding box, future dates, and sales before the date exactly two calendar years earlier. Sizes must be 2,000–3,000 sqft inclusive and bedrooms at least two. Source single-family classifications do not independently establish physical detachment. Known attached homes are excluded, as are Redfin URLs identifying units. Cross-source merging uses provider-qualified IDs and canonical street/city. Missing street suffixes require coordinates within 25 m; conflicting explicit suffixes remain distinct. Newer sales replace older ones. Same-date price, area, or bedroom conflicts retain existing data and are counted. Atomic publication and a shared import/refresh lock protect the saved map.

## Estimate

Time weights are 1.00 for 0–30 days, 0.80 for 31–90, 0.55 for 91–183, 0.30 for 184–365 and 0.15 for the remainder of two calendar years. The six-month bucket is approximated as 183 days; the final two-year cutoff uses the calendar date. All dates and current-day filtering use UTC.

Choose the smallest of 0.75, 1.5, 2.25 or 3 km containing at least eight distinct eligible homes. If none does, use the homes within 3 km. For every comp:

```
weight = time_weight
       * exp(-0.5 * (distance_km / (radius_km / 2))^2)
       * exp(-0.5 * (ln(comp_sqft / subject_sqft) / 0.25)^2)
       * bedroom_weight

bedroom_weight = 0.75                       if subject_beds >= 3 and comp_beds == 2
                 0.9^abs(comp_bucket - subject_bucket) otherwise
bedroom_bucket = min(5, bedrooms)           # 2, 3, 4, or 5+
```

The bedroom selector changes the relative weight of comparable sales in both the address estimate and map shading. It softly favors similar bedroom counts without excluding other eligible homes. For the default three-bedroom subject, three-bedroom comps receive 1.00, four-bedroom comps 0.90, five-or-more-bedroom comps 0.81, and two-bedroom comps 0.75. A two-bedroom subject gives two-bedroom comps the full 1.00 bedroom weight. All counts of five or more share one bucket. These are similarity weights, not a learned or causal bedroom price premium: selecting more bedrooms can raise or lower the estimate according to the nearby sales.

At least three distinct homes and an effective sample count of at least two are required. Effective count is `(sum(weights)^2) / sum(weights^2)` and reflects concentration, not statistical independence. Values are withheld beyond 3 km or when evidence is insufficient.

Compute the weighted median of log(price/sqft), then its weighted median absolute deviation. Clip extreme log values at the median ± `max(0.18, 3 * 1.4826 * MAD)`, calculate the weighted mean and exponentiate. This reduces the influence of isolated extreme sales. The displayed range is the weighted 20th–80th percentile of unmodified comparable prices: a descriptive sale spread, not a formal confidence interval or price forecast. Limited support is flagged below eight comps, below five effective comps, when the weighted median comp age exceeds 183 days, or when the nearest comp is over 1 km away.

The model does not adjust for renovation, condition, lot size, views, street, school district, financing concessions, or the market's movement since each sale. It can combine nearby homes across neighborhood boundaries. Recency weights are a configurable judgment, not coefficients learned from historical sale predictions. Total estimated price simply multiplies estimated $/sqft by the requested size.

## APIs

- `GET /api/data`: eligible sales in one `sales` array, coverage, last successful data timestamp, model details, and exclusion diagnostics. HTTP gzip is supported; internal aliases are not duplicated on the wire.
- `GET /api/estimate?lat=37.8&lng=-122.25&sqft=2500&beds=3`: estimate or explicit insufficient-data state, actual comparables and each weight component.
- `GET /api/suggest?q=<partial address>`: saved-sale matches plus bounded Photon / OpenStreetMap suggestions after at least six characters. Partial query text may be sent to Photon; approximate place results are labeled. Responses include `results`, `remote_status`, attribution, and a privacy notice.
- `GET /api/geocode?q=<full street address>`: submitted lookup first reuses precise saved-sale and Photon matches, requiring the same house number, complete street and supplied locality. Ambiguous cities remain choices and street-level suggestions cannot silently resolve a home. Otherwise it falls back to the [U.S. Census Geocoder](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html), Census results cached for 90 days and empty results for five minutes, with at most 512 cached queries. Outbound requests are serialized at no more than one per second. Census coordinates may be interpolated along an address range. No autocomplete, bulk geocoding, or background search. Addresses are sent to Census only on search submission. Search query strings are omitted from this server's access log; the local cache is mode 0600.
- `GET /api/refresh`, `POST /api/refresh`: status and explicitly requested background refresh via `refresh.RefreshManager`.
- `POST /api/import/redfin`: same-origin JSON `{csv, filename}`, bounded to 5 MiB, for one manually downloaded CSV. Requires explicit PAST SALE, Sold status, sale date, price, area, bedrooms, coordinates and source link. Returns import counts and partial/manual-source status. No Redfin cookies or automated scraping are used. Redfin export omissions remain a coverage limitation.
- `GET /api/provider`, `POST /api/provider`: sanitized status and provider configuration, delegated to the refresh manager.
- `GET /api/health`: process health only; not a claim of data freshness or completeness.

All requests require an allowed host: localhost/loopback by default. Additional explicit hostnames can be configured with `HOMES_ALLOWED_HOSTS` (comma separated). Mutating requests require JSON and a same-origin `Origin` header. A loopback reverse proxy may supply `X-Forwarded-Host`, which is also checked against the allowed names. Host checks do not provide user authentication. The process never initiates periodic refresh itself. No live API key is sent to the browser or placed in `public/`. The external-refresh implementation owns provider budgets, atomic data updates and sanitized errors.
