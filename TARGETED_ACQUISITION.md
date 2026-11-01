# Targeted gap acquisition

`scripts/targeted_api.py` is an operator tool. It stages data from one explicitly
selected geographic search, using a **new independent grant and ledger**. It
does not read or update the application's old grant, publish rows, change an
account plan, purchase credits, schedule future work, or retry uncertain starts.

## Pricing and funding

Checked on 2026-10-31 using [the public actor metadata](https://api.apify.com/v2/acts/topapi~zillow-scraper)
and [the actor's input documentation](https://apify.com/topapi/zillow-scraper/input-schema):
`topapi/zillow-scraper` charges $0.00085 per returned listing, plus $0.0016 when
property details are delivered. Detailed rows cost up to **$0.00245 each**.
There is no start event. The runner pins build `0.1.23` and verifies the current
event pricing before each new run. Details supply exact sale history and
attachment evidence that a search card can omit. Accepted rows still pass the
existing date, size, bedroom, sold-price and detached-home validation.

The default maximum allocation is **$8 actor caps + $1 platform overhead + $1
tax/nonusage headroom = $10 total cash exposure** when usage has no existing
credit. At those rates, $8 could purchase at most 3,265 detailed rows. This is
not a forecast of new eligible homes: duplicates, missing fields and provider
failures can greatly reduce the usable gain. All caps remain reserved even when
a run fails or spends less; unused caps are never automatically redistributed.

Apify's [ordinary Free plan cannot buy extra credits](https://help.apify.com/en/articles/5136728-subscribing-to-the-apify-platform).
The current [Starter plan is $19/month](https://apify.com/pricing), already above
this campaign's $10 incremental cash ceiling. Do not subscribe without separate
approval. A [prepaid AGI token](https://agi.apify.com/AGENTS.md) has a $1 minimum,
a fixed spend cap and a 14-day lifetime, but requires a supported funded wallet;
unused balance is nonrefundable. The existence of that route is not proof that
a wallet is configured. A verified free-credit balance can instead fund a
smaller plan at no additional cash cost.

Actor caps cover individual run event charges. Post-run dataset reads, transfer
and retained storage are separately metered. The $1 reserve is deliberately
large relative to this runner's 64 MiB download and 300-request ceilings; it is
not an account-wide guarantee. Verify the merchant's available balance/usage
limit and absence of other consumers. Export and audit the purchased data, then
remove only this campaign's remote temporary storage after final billing is
checked. Never delete older campaign evidence automatically.

## Manifest

Store the real manifest, grant, ledger and acquired data outside public version
control. This example uses an intentionally generic target label:

```json
{
  "version": 1,
  "campaign_id": "gap-audit-example",
  "as_of": "2026-10-31",
  "overhead_reserve_usd": "1.00",
  "targets": [{
    "id": "target-a",
    "label": "Target A",
    "search": {"bounds": {"north": 37.8, "south": 37.78, "east": -122.2, "west": -122.22}},
    "days": "12m",
    "max_results": 300,
    "actor_cap_usd": "0.735"
  }]
}
```

Alternatively, use `"search": {"location": "City name, CA"}`. A manifest
contains 1–20 targets, each at most 1,000 rows and $2.45. Rectangle edges must be
within the Bay Area, and width/height at most 0.25 degrees. Search windows are
30 days, 90 days, 6 months, 12 months or 24 months. Every search requests sold
houses, 2+ beds, 2,000–3,000 sqft, full details, newest order, geographic
subdivision and two concurrent provider connections. No monitoring is enabled.

Validate without network or state changes:

```sh
python3 scripts/targeted_api.py /private/campaign/manifest.json
```

Record the resulting manifest SHA-256 in a **new**, owner-only grant:

```json
{
  "version": 1,
  "campaign_id": "gap-audit-example",
  "manifest_sha256": "<validation output>",
  "campaign_directory": "/private/campaign/staging",
  "enabled": true,
  "expires_at": "<timezone-aware deadline within 24h of verification>",
  "cash_limit_usd": "10.00",
  "fixed_cash_paid_usd": "0.00",
  "available_usage_credit_usd": "<verified unused credit>",
  "tax_and_nonusage_reserve_usd": "1.00",
  "merchant_usage_limit_verified": true,
  "funding_verified": true,
  "account_verified_at": "<actual timezone-aware verification time>",
  "funding_kind": "free_credit"
}
```

`funding_kind` is `free_credit`, `prepaid_token`, or `paid_account`. Verification
flags must describe actual merchant evidence, not intended setup. The immutable
grant calculation is `fixed cash + max(0, planned usage - available credit) +
nonusage reserve <= cash limit <= $10`. Free and prepaid funding must cover all
planned usage. Consequently, recording a new $19 plan charge cannot pass.

Explicit acquisition (one target, then stops):

```sh
python3 scripts/targeted_api.py /private/campaign/manifest.json \
  --execute-target target-a --grant /private/campaign/grant.json \
  --credential /private/credential.json --campaign-dir /private/campaign/staging
```

The credential file contains `{"api_key": "..."}` and must be owner-only. Never
pass a token on the command line. An existing credential can be used; this grants
no permission from an earlier campaign. The new manifest, grant and credential
fingerprint are bound to the ledger and cannot change after initialization.
The grant must name the absolute canonical campaign directory, with symlinks and
`..` resolved. A different `--campaign-dir` is rejected before creating files or
making requests, so the same grant cannot silently acquire a fresh ledger by
changing the output folder. Keep this private grant with its one authoritative
ledger; do not copy or alter it to authorize another campaign.

## Recovery and publication

The private ledger is locked, atomically written and directory-synced. It records
start intent **before** the POST, then the returned run ID. API calls and maximum
response bytes are reserved before dispatch and survive interruption. A repeat
invocation resumes a saved run ID; it never resurrects or restarts the run.
Another target cannot start while any run is active or a start is uncertain.
Execution is limited to 30 minutes per remote run, shortened to fit the grant.

If the start response was lost, stop and reconcile the merchant's run list/input
and start time. There is intentionally no automatic unlock or second start.
If an ID was returned but its limits were unconfirmed, a later invocation only
reads that same ID and requires the original limits before continuing.

A successful acquisition produces `<target-id>.json` with normalized sales,
explicit attached-home exclusions, provenance and pagination diagnostics. It
remains partial even if the dataset downloaded fully. Audit new identities,
sale-price precision, eligibility, coverage improvement and held-out error
before merging through the ordinary data pipeline. Keep the original rows and
exclude attached properties using the normal provenance validation. Rerun the
coverage audit after each stage and stop when extra rows add little evidence.
