# Quality and performance review

The application is deliberately small: standard-library Python, plain JavaScript, a web worker for the price surface, and locally vendored map assets. No build step is required.

## Verification

- Python tests cover eligibility, dated sold-price extraction, deduplication, estimates, provider budgets, interrupted runs, imports, HTTP validation, caching, and geocoding failures.
- JavaScript tests cover keyboard suggestions, mobile sheet dragging, stale-response cancellation, provider controls, refresh monitoring, and worker parity with the Python model.
- CI runs on Linux with Python 3.9 and 3.13 and on macOS with Python 3.13, using Node.js 22. Tests use synthetic sales and fake provider clients; shoreline checks also use bundled public geographic boundaries.
- Manual Chromium review covers 1440 × 900 and 1024 × 768 desktops and a 390 × 844 mobile viewport: search with arrow keys/Enter, subject controls, map navigation, sale markers, source/data dialogs, Escape dismissal, empty data, and dragging the result sheet after an estimate.
- A publication guard checks for private files and recognizable secrets. Full staged content and fresh Git history are also reviewed before initial publication.

## Improvements measured

A 21,050-record workload was used for server measurements; figures are warm median CPU times, not network or page-load timings. First dataset preparation still takes around 2.6 seconds on the review machine.

| Operation | Before | After | Change |
| --- | ---: | ---: | --- |
| Dataset JSON and gzip preparation | 426.4 ms | 0.013 ms | Reuse encoded snapshot until data or eligibility date changes |
| Local estimate | 52.2 ms | 8.57 ms | Reject distant candidates before detailed scoring |
| Local address suggestions | 43.2 ms | 0.026 ms | Index the current snapshot |
| Surface worker, synthetic 21,050 sales / 1,800 samples | 1,682 ms | 840 ms | Cache per-home similarity and transfer packed coordinates |

The optimized estimate matched the previous calculation exactly in 27 comparisons across nine locations and three bedroom choices. The worker benchmark produced identical Float32 surface values. Timings vary by device and load; these comparisons do not establish prediction accuracy.

Run the standalone synthetic surface benchmark with:

```sh
node tests/benchmark_surface.js
```

An optional baseline worker file can be supplied as its first argument for a local comparison.

## Design and reliability changes

Supporting text has stronger contrast; dialogs have accessible names; region selections expose pressed state. Mobile details can be expanded and collapsed using the handle or keyboard. Old estimates are hidden immediately when the subject changes, and obsolete requests are cancelled. Map pans reuse formatters and bounds, and sale popups are created when opened. The last price raster stays geographically anchored during dragging and zooming; a 20% margin on each edge covers nearby movement, and a fresh raster replaces it after movement ends. Large moves can expose territory outside that margin until recalculation completes. The interface reports partial coverage and the 3,000 visible-sale-dot cap explicitly.

Snapshot responses reuse serialized data; static assets support conditional requests. The map observes its actual container dimensions, including mobile viewport-height changes, and applies its visual filter to each tile instead of the moving tile pane. Synthetic touch pan/pinch and viewport-growth checks found no uncovered tile samples after loading; this does not reproduce every physical iPhone/WebKit compositing issue. HTTP host/origin validation, request framing, body limits, and timeouts protect the local API boundary. Address caches are bounded, and malformed datasets or provider responses produce actionable errors.

## Remaining limits

This review is not a formal screen-reader or WCAG conformance audit. Native touch hardware, Safari, and Firefox were not exercised. Automated drag tests and a mobile browser viewport complement desktop interaction checks; physical pinch gestures were not tested. The estimate is a comparable-sales model without out-of-sample valuation validation. Provider completeness and listing classification remain data-quality limitations. Public services still determine remote geocoding and tile latency.

## Interaction follow-up

An independent interaction review led to three mobile drawer snap positions, a persistent tap control, native content scrolling, keyboard snap controls, and shared map-overlay geometry. Search and map selection now keep the selected marker above the expanded drawer. Manual minimization cancels a pending address lookup, and manual content interaction prevents late estimate scrolling. Reduced-motion settings cover map movement and pinch release; tile fades are disabled. See [the interaction review](INTERACTION_REVIEW.md) for the findings and browser evidence.

Submitted addresses reuse precise local and Photon matches before Census fallback, while preserving ambiguous choices and distinguishing outages from no matches. Coverage diagnostics report imported city counts and recorded provider gaps without exposing addresses or provider IDs. The model continues withholding estimates where too few nearby sales exist.

## Shoreline rendering

The final smoothed price raster is clipped with bundled 2025 Census TIGER/Line water polygons, using the same Web Mercator bounds as its Leaflet overlay. This preserves islands and keeps the clipped shoreline attached during pan and pinch gestures. Shading waits for the boundaries and stays disabled if they fail to load or draw; address search remains available. Tapping mapped water withholds a home estimate. The mask is static public geography, not additional property data, and does not change comparable-sale weights or claim to fix missing sales.

Static text/JSON assets now support cached gzip encoding, conditional requests, and HEAD responses. The 3.7 MB boundary asset transfers as about 1.17 MB gzip. Automated tests cover geographic island/water samples, Mercator alignment, overlapping water polygons, render scheduling, failure handling, and gzip negotiation/cache invalidation.

Chromium verification at 1200 × 839 and 393 × 760 confirmed clear Bay water and shaded land/islands. Held touch drag and pinch retained the clipped raster; a water tap and a subsequent bedroom change both kept the explicit water message. The browser reported no console errors. These are emulated touch checks, not physical iPhone verification.
