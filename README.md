# Bay Atlas

A self-hosted, touch-friendly map of Bay Area sold-home prices per square foot. The color surface is continuous: it estimates nearby sale values rather than showing sale density.

**This repository contains application code and synthetic test fixtures only.** It starts with an empty map. Supply your own permitted sale data or provider account; no account, API credit, listing dataset, or hosted deployment is included.

## Run locally

Requirements: Python 3.9 or newer. Node.js 18 or newer is needed only for the JavaScript tests. The application has no third-party Python dependencies or frontend build step.

```sh
python3 server.py --port 8788
```

Open [http://127.0.0.1:8788](http://127.0.0.1:8788). Choose **Update data** to import a supported Redfin sold-home CSV or configure a Zillow data provider. A fresh checkout displays an honest empty state until valid records have been imported.

## Explore

- Pan, pinch, and zoom; choose the East Bay or all nine Bay Area counties.
- Enter part of an address, choose a suggestion, or click a point on the map.
- Adjust the subject home from 2,000 to 3,000 square feet and 2, 3, 4, or 5+ bedrooms.
- Inspect the estimate, descriptive comparable-price range, and linked source sales.
- Refresh data on demand. There is no recurring data collection job.

The target is detached single-family homes with at least two bedrooms. Recent sales receive the most weight; records older than two calendar years are excluded. Sparse areas remain unshaded. Bedroom count is a similarity preference, not an assumed price premium.

Coverage depends on the data supplied. A completed county search does not prove every sale was returned, and single-family classification does not independently establish physical detachment. Explicit attached-home evidence is excluded. Estimates do not account for condition, renovation, lot size, school boundaries, views, or concessions. See [the model details](README-model.md).

## Add data

**Manual CSV:** download a supported Redfin sold-home export with House, 2+ bedrooms, 2,000–3,000 square feet, and sales within the last two years. Import it through **Update data**. Only explicit dated sold records are accepted. Availability and export limits depend on the source and market. The app does not automate Redfin login or read browser cookies.

**Provider API:** the Apify adapter uses the `topapi/zillow-scraper` actor. The older HouseData/RapidAPI adapter is also supported. These are third-party services; this project is not affiliated with Zillow, Redfin, Apify, or RapidAPI. Use an account and data rights appropriate to your use. Provider settings start unconfigured, and paid execution is disabled by default. Review [provider setup and spending controls](PROVIDERS.md) before enabling requests.

Data, snapshots, usage ledgers, and geocoding caches are created under `data/`; provider credentials are stored under `private/`. Both directories are excluded from Git. Do not place sensitive files under `public/`, the only directory served as static assets.

## Deploy privately

The server binds to loopback by default. It has **no built-in user authentication**: put it behind an authenticated reverse proxy before making it reachable from another device. Allow only your chosen proxy hostname:

```sh
HOMES_ALLOWED_HOSTS=map.example.org python3 server.py --port 8788
```

The frontend supports the root path or the `/homes/` prefix. Configure the proxy to forward to the loopback server. Host and same-origin checks reduce unwanted requests; they do not replace authentication. Keep provider credentials and imported sales out of publicly accessible directories and logs.

Address autocomplete sends partial address text to Photon; submitted full-address geocoding uses the U.S. Census Geocoder. Browser map tiles come from OpenStreetMap. See [privacy and publication boundaries](PRIVACY.md).

## Verify changes

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_*.js
python3 scripts/check_public_tree.py
```

Tests use fake network clients and synthetic properties. They require no provider credentials and do not spend API credits. The public-repository audit reports file names, line numbers, and finding categories without printing matched sensitive values. It is an additional check, not a substitute for reviewing the complete Git diff and history before publication.

After a county import, run the local data audit:

```sh
python3 scripts/audit_import.py --root .
```

It makes no API calls. Exit code 0 means the saved data passes the audit and the current generation is published; 1 means a data or coherence failure; 2 means publication is incomplete. A successful audit does not establish exhaustive market coverage or prediction accuracy. Audit output may contain private operational metadata; keep it out of Git.

## Project layout

| Area | Purpose |
| --- | --- |
| `server.py` | Standard-library HTTP service, data snapshots, geocoding, API boundary |
| `model.py` | Eligibility rules, merge identity, weighted estimates |
| `refresh.py`, `apify_refresh.py`, `apify_source.py` | Provider configuration, bounded requests, checkpointed imports |
| `redfin_csv.py` | Supported manual CSV validation |
| `address_suggest.py` | Local and Photon address suggestions |
| `public/` | Responsive UI, locally vendored Leaflet/fonts, map worker |
| `tests/` | Offline unit and interaction tests with synthetic fixtures |
| `scripts/` | Read-only import and publication audits |

Application code is available under the [MIT License](LICENSE). Third-party notices and licenses are retained in `public/vendor/`. The application license does not grant rights to map data or source listings; those retain their respective attribution and terms.
