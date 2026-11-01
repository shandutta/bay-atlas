# Evidence, accuracy and map resolution

Bay Atlas estimates the typical sold price per square foot of a 2,000–3,000 sqft detached single-family home with at least two bedrooms. A smooth color field is an interpolation of nearby sales, not a measurement of every property. The point estimator, recency weights and maximum 3 km search radius remain as described in [README-model.md](README-model.md).

The internal `evidence` object describes whether a location meets a data-acquisition target. It is deliberately separate from a probability that an individual home will sell within a particular range. Condition, lot, renovation, street, schools and omitted listings can matter even when many comparable sales are present.

## Acquisition target

A location meets the target only when every condition below holds. These thresholds are operating criteria supported by a retrospective check; they are not a calibrated confidence interval.

| Measure | Target | Purpose |
| --- | ---: | --- |
| Effective comparable homes | At least 12 | Detect when a few sales dominate the estimate |
| Sales within 183 days | At least 4 | Retain recent evidence |
| Effective 250 m spatial blocks | At least 4 | Reduce apparent precision from a concentrated group of homes |
| Directional support | At least 3 quadrants, each with at least 10% of weight | Flag one-sided extrapolation |
| Radius containing 80% of weight | At most 1.5 km | Keep the evidence local |
| Weighted median age | At most 183 days | Avoid an estimate mostly supported by older sales |
| Log standard-error proxy | At most 0.08 | Flag sampling variability and concentration |
| Largest removal-of-one-block shift in the raw log mean | At most 0.10 | Flag dependence on a single geographic cluster |

An intermediate `usable` tier requires eight effective homes, three recent sales, three effective blocks, a radius of at most 2 km, median age at most 183 days, a log standard-error proxy at most 0.12 and a block shift at most 0.15. Other available estimates are `limited`. The estimator's existing minimum of three homes and two effective homes remains the boundary for producing a point estimate. The tier does not change that estimate or silently expand its search radius.

For normalized comparable weights `p_i`, the effective home count is `1 / sum(p_i²)`. Spatial block weights are the sum of home weights in each fixed 250 m block; the effective block count uses the same formula. Blocks group nearby observations but are **not proven statistically independent**.

Let `y_i = log(price_i / sqft_i)`, `m = sum(p_i * y_i)`, and `g_b = sum(p_i * (y_i - m))` within block `b`. For `B > 1` occupied blocks, the block standard-error proxy is `sqrt(B / (B - 1) * sum(g_b²))`. The reported proxy is the larger of that value and `1.4826 * weighted_MAD(y) / sqrt(min(effective_homes, effective_blocks))`. A single block fails the target. Block sensitivity is `max(abs(g_b / (1 - block_weight_b)))`.

These diagnostics use the **raw weighted log mean**, so the sensitivity is not an exact refit of the robust, clipped price estimator. They omit systematic error and source-selection bias. They must not be converted into a nominal 95% price interval. The existing displayed 20th–80th percentile range remains a descriptive comparable-sale spread.

## Three separate resolutions

1. **Coverage audit:** 500 m cells containing observed eligible-home locations. A representative home is excluded from its own estimate. The denominator is the known residential evidence footprint, not every square kilometer of the Bay Area; entirely unseen residential pockets can still be missed.
2. **Statistical support:** twice the radius containing 80% of comparable weight. This is the diameter of the area supplying most of the estimate, not a proven ability to resolve price differences over that distance. The later validation period had a median support diameter of approximately 1.1 km.
3. **Shading:** a world-anchored grid with a minimum nominal spacing of 250 m, using nested coarser levels when zoomed out. The finest grid is approximately 246–253 m on the ground across the Bay Area. Bilinear interpolation makes continuous colors between samples. Extra screen pixels and deeper zoom do not create new price evidence. Water masking still applies after smoothing.

An address estimate is calculated at the requested location using the point model. Map colors use the geographic sampling grid, so they can differ from the exact point estimate, sometimes materially near a sharp local transition or a change in the selected comparison radius. The address estimate remains the authoritative point calculation. A local minimum-support check prevents the coarse grid from painting an unsupported center even when its corners have data.

## Retrospective validation, October 2026 snapshot

The baseline snapshot before targeted acquisition contained 21,954 eligible homes. The analysis used 6,160 held-out sales from May through October 2026. For each sale, the estimator used only **strictly earlier** eligible sales; the subject property, same-day sales, future sales and sales older than the two-year window were excluded before choosing the radius. Sale ages and recency weights were recomputed at the prediction date. The subject's actual area and bedroom count were used.

May–July served as the development window; August–October was the later check. Across both windows, 6,115 estimates were available, with median individual-home absolute percentage error of 10.5% and an 80th-percentile error of 23.3%.

Results for the later check:

| Evidence tier | Available predictions | Median absolute error | 80th-percentile error | Predictions within ±20% |
| --- | ---: | ---: | ---: | ---: |
| Target met | 492 | 9.5% | 19.3% | 80.9% |
| Usable | 612 | 10.8% | 21.5% | 76.8% |
| Limited | 1,573 | 10.8% | 25.8% | 71.6% |
| All available | 2,677 | 10.6% | 23.4% | 74.5% |

Another 23 later-period sales lacked enough earlier evidence for any estimate. The operating accuracy goal is approximately 80% of later individual-home predictions within ±20%, while retaining local evidence. The target tier reached that observed rate, but the rate is uncertain: resampling 2 km spatial blocks produced a 95% bootstrap range of approximately **77.1%–84.7%**. This is uncertainty in the aggregate validation rate, not an individual-home prediction interval, and spatial dependence can extend beyond those blocks.

Neighborhood-average precision is harder to validate. Only 17 target-tier 500 m cells had at least three held-out sales in the later period. Their median cell residual had a median absolute error of 8.6% and an 80th-percentile error of 13.3%. That sample is too small and selective to certify a universal neighborhood-average confidence level; the sampling proxy remains a diagnostic.

Repeating the same protocol after targeted acquisition produced 2,720 available later-period predictions. The target tier contained 493 homes: median error 9.46%, 80th-percentile error 19.64%, and 80.73% within ±20%, with a spatial bootstrap range of 76.50%–84.67%. Accuracy was essentially unchanged. The larger evaluated population also changed, so the additional 43 predictions are not a controlled accuracy improvement. The final private snapshot contained 22,191 eligible homes as of November 1 UTC.

The current snapshot contains the latest observed sale for each property. It is not a complete archive of which records would have been available on each historical date. Missing listings, overwritten older resales, provider filters and geographic selection can bias the check. Adding sales can improve evidence counts without necessarily improving accuracy. Repeat this analysis after material dataset or model changes.

## Why not just widen the search?

The same later-period check compared larger pools while preserving the price and time-weight formulas. Increasing the stopping rule to 12 effective homes raised the share meeting the evidence target but nearly doubled median support diameter from 1.10 km to 2.10 km; median prediction error changed from 10.56% to 10.74%. Expanding until the complete target was met produced median diameter 2.37 km and median error 11.08%.

Wider pooling therefore did not demonstrate an accuracy gain. The acquisition plan should seek genuinely missing **nearby** sales, especially recent observations, rather than increase the displayed confidence by borrowing distant neighborhoods. The point model retains its existing radius selection.

## Reproducing the check

Run the generic backtest against a private sales snapshot. Dates are explicit so results do not drift with the clock:

```sh
python3 scripts/backtest_confidence.py --data data/sales.json \
  --start 2026-05-01 --validation-start 2026-08-01 --end 2026-10-31 \
  --output artifacts/confidence-validation.json
```

The script makes no network calls and emits aggregate counts, errors, bootstrap rates and an input-file hash. Individual addresses and predictions are not included in the report. When historical rows contain multiple sales for a property, each prediction uses only that property's latest earlier sale; all sales of the held-out subject itself are excluded. Same-day observations are also excluded before radius selection. Test coverage includes those leakage cases and the two-calendar-year boundary.

## Shading fidelity check

On 500 deterministic occupied-cell probes from the acquisition preview snapshot, a 250 m grid had a median absolute deviation of 0.34% and a 95th-percentile deviation of 3.72% from the exact point model. A 500 m grid had 0.91% and 5.94%, respectively. The 250 m grid retained 497 supported probes versus 496 at 500 m; neither colored an unsupported probe. This supports the 250 m minimum rendering scale.

The largest deviations were 29.2% and 25.7%. These outliers show why display resolution is not statistical precision, and why an address query should use its exact estimate. These are rendering comparisons, not errors against actual sale prices. The cold 250 m pass took about 800 ms for these dispersed probes and a repeat pass about 174 ms; timing depends on hardware, view and cached samples.
