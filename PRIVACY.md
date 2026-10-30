# Privacy and publication boundaries

The public repository contains code, documentation, third-party notices, and synthetic tests. It excludes imported property records, search history, provider credentials, usage ledgers, invoices, screenshots, private deployment details, and historical commits from any private installation.

## Local state

`private/` contains provider configuration and secrets. `data/` contains imported sales, snapshots, refresh state, provider reservations, and the submitted-address geocoding cache. Neither directory belongs in Git. Audit output can contain local paths, run identifiers, property identities, and sampled coordinates; treat it as private operational data too.

Only `public/` is served as static files. API responses deliberately expose loaded sale records and estimates to anyone who can access the application. The server does not include user authentication, so protect access with an authenticated reverse proxy. Host and same-origin checks alone are not access control.

Request logging omits query strings, including searched addresses. Provider status responses omit API tokens. Address autocomplete queries are cached in bounded process memory. Successful submitted-address geocoding results may be cached on disk for up to 90 days; empty results expire after five minutes. The cache is bounded to 512 queries.

## Outbound requests

| Action | Recipient and data |
| --- | --- |
| Browse the map | OpenStreetMap tile service receives tile coordinates and normal browser connection metadata |
| Type enough address text for suggestions | Photon receives the partial query and Bay Area search bounds; saved sales are also searched locally |
| Submit an address | Saved sales are checked locally; Photon receives the query when needed. If no precise house/street match is found, U.S. Census Geocoder receives the submitted address |
| Request an API refresh | The selected provider receives its credential and requested search parameters |
| Open a source sale link | Zillow or Redfin receives a normal browser page visit |
| Import a CSV | The file is sent to this application; the importer does not forward it to a provider |

Leaflet and fonts are served locally. There is no analytics integration. Public geocoding and tile services have their own terms and privacy policies.

## Before publishing a fork

1. Start from a clean checkout of this public repository. Do not copy the Git history from a private deployment.
2. Keep private configuration, data, screenshots, and artifacts outside the repository or in ignored directories. Review `.gitignore`; ignoring a file does not remove it from existing commits.
3. Run `python3 scripts/check_public_tree.py`. In a Git repository it scans tracked paths, including files already tracked despite ignore rules. Before Git initialization it scans the export tree while excluding local runtime directories.
4. Review `git diff --cached`, tracked file names, author metadata, commit messages, remotes, and all history that will be pushed. The scanner is intentionally limited and cannot recognize every personal detail or leaked secret.
5. Use synthetic records in tests. Never promote downloaded listing fixtures or a real searched home into a public test case.

The audit prints finding categories and file/line locations only, avoiding echoed secrets. It detects common private-file locations, home-directory paths, private network hostnames, email addresses outside third-party notices, and recognizable credential patterns. It is not a guarantee that a repository contains no sensitive information.

Third-party license notices may legitimately contain their authors' contact details. Keep those notices intact; do not mistake their attribution for installation-specific personal data.
