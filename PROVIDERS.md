# Provider setup and bounded refreshes

Bay Atlas starts without credentials or a dataset. Refreshes occur only when requested through the UI. This application neither purchases a subscription nor cancels one, and it does not create a recurring provider schedule.

## Manual Redfin CSV import

Use a supported export from a sold-home search. The importer requires an explicit past sale, sold status, sale date, price, size, bedrooms, coordinates, and an allowlisted source link. Upload one file of at most 5 MiB through **Update data**. Valid rows are merged atomically; snapshots and import provenance remain local. Missing dates, ineligible sizes, attached-home evidence, and conflicting same-date values are reported rather than guessed.

## Zillow through Apify

The adapter uses `topapi/zillow-scraper` through Apify's API. It searches the nine Bay Area counties with a two-year sold window, 2+ bedrooms, and 2,000–3,000 square feet. Detailed property history is requested by default. A matching dated Sold event can supply an exact price; asking prices and Zestimates cannot.

1. Check your current plan, available credit, actor pricing, and account usage limit directly with the provider.
2. Open **Update data**, choose **Zillow via Apify**, and save your own API token.
3. For free mode, verify that the account is on a Free plan with paid billing disabled, then confirm that in the form. This confirmation is an operator assertion, not an automatic account audit.
4. Set an allowance backed by the verified available free credit. Small allowances can yield partial searches or no eligible records. Start **Refresh sales** only when ready to consume that allowance.

The initial free-mode defaults are a $0.10 local credit allowance, a $0.08 actor-run cap, and one run per refresh. These are application settings, not a claim about the provider's current free allocation or prices. Check [Apify pricing](https://apify.com/pricing) and the [actor page](https://apify.com/topapi/zillow-scraper) for current terms.

Tokens are saved in `private/provider.json`, with restrictive file permissions. They are never returned in status responses or saved in browser storage. Replacing a token clears its verified status and disables any paid allowance.

## How the spending controls work

Each actor cap and API/storage reserve is committed before dispatch and is not automatically refunded when a run costs less. HTTP calls and the maximum possible response bytes are persisted before requests. Interrupted or uncertain reads retain their full reservation. This conservative approach prevents a process restart or repeated Refresh click from reclaiming already-committed allowance.

`data/apify-state.json` records reservations and county progress. Staged results are under `data/apify-cache/`. Keep these files together when backing up or restoring the application. Deleting or rewriting them can invalidate the local spending controls.

Apify receives `maxTotalChargeUsd` for actor execution. Reads, transfer, and storage can incur separate usage. The app's limits do not cover other activity in the same provider account and are not an account-wide billing guarantee. Merchant invoices and account controls remain authoritative.

Unknown start outcomes require reconciling the provider run history; the app must not blindly start a replacement. Known run IDs resume after interruption. One timed-out paid run may be resumed once with the same run and dataset IDs, the unchanged cumulative cost cap, and an existing allowance. Recovery intent is saved before dispatch; an uncertain response allows a status check, not another recovery attempt. See [Apify run limits](https://docs.apify.com/actors/running/runs-and-builds#resurrect-a-run-that-reached-its-cost-limit).

## Optional operator-provisioned paid allowance

Paid mode is disabled by default and cannot be enabled or enlarged in the browser. An operator must provision an explicit `apify_paid_allowance` in the private configuration after independently verifying payment and the account usage limit. It is tied to one import generation, has a timezone-aware expiry, and cannot be reset through Refresh.

The validator currently enforces a maximum total of $75 and at least $1 reserved for taxes or nonusage costs. Budget fields are:

| Field | Meaning |
| --- | --- |
| `total_budget_usd` | Maximum authorized cash amount |
| `fixed_cash_paid_usd` | Plan or other fixed cash already paid |
| `included_usage_credit_usd` | Verified credit available for this import |
| `tax_nonusage_reserve_usd` | Separate headroom for taxes and nonusage charges |
| `usage_budget_usd` | Total actor and API/storage reservations |

The required relationship is:

```text
fixed_cash_paid + max(0, usage_budget - included_usage_credit)
    + tax_nonusage_reserve <= total_budget
```

Additional fields include `enabled`, a unique `id`, `expires_at`, `payment_verified`, and `account_limit_verified`. These confirmations represent the operator's verified evidence; the code cannot establish them itself. Actor caps plus at least $0.25 of API/storage reserve for each county must fit the usage budget. Do not copy another installation's grant, ledger, or run IDs.

When the allowance expires, a refresh may still publish already-cached successful counties without provider requests. Publish and audit those results before disabling the grant. Any paid subscription must be managed separately in the provider account; disabling the local grant does not cancel merchant renewal or settle invoices.

## Coverage and completion

The adapter validates explicit sale dates and prices, rejects attached properties when the evidence says so, and reapplies the current UTC two-calendar-year cutoff during publication. A successful search can still omit properties. Provider result caps, unreachable listings, missing sale evidence, cost limits, and partial county searches must remain visible as coverage limitations.

`python3 scripts/audit_import.py --root .` inspects saved rows, county checkpoints, publication identity, and descriptive sample estimates without network requests. It checks internal validity and coherence, not whether every home was collected or whether the estimates predict future sales accurately.

## HouseData / RapidAPI

The older adapter has per-refresh and rolling 31-day request limits. City-based searches and source result caps cannot establish full county coverage. Provider credentials are isolated, and replacing a key clears verification. Obtain and verify any subscription separately before configuring it.
