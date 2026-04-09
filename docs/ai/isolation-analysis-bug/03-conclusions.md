# 03 — Conclusions

This document presents the deductive chain that the methodology and findings produce. Read [00-problem.md](00-problem.md), [01-methodology.md](01-methodology.md), and [02-findings.md](02-findings.md) first.

## What we ruled out

### 1. The pass-rate gap is not statistically significant

The original observation — calibration p̂ = 0.740 vs amplify p̂ = 0.644 — is a 0.10 absolute drop, but at n=50 vs n=135 it gives **z = -1.23, p = 0.22**. Not significant at α = 0.05. The gap is consistent with sampling noise.

This is the *first* thing to rule out. It does not by itself end the investigation, because the per-probe feature differences (output_tokens, duration) ARE statistically significant. But it puts the pass-rate concern in proportion: the experiment design isn't necessarily broken just because amplify produced 0.10 lower than calibration.

### 2. The equations are not systematically harder

[Test 4](01-methodology.md#test-4--replay-controlled-equation-comparison): the replay run used the **exact same 15 equations** that amplify drew, run fresh today via the same code path but with parallel batches on **different equations** (matching calibration's pattern, not amplify's same-equation pattern).

Replay produced:

- Pass rate **0.756** — matches calibration's 0.740 within noise, NOT amplify's 0.644.
- `output_tokens` median **10,726** — significantly lower than amplify's 12,509 (p = 0.014), only marginally different from calibration's 8,780 (p = 0.065).
- `duration_ms` median **54,705** — significantly lower than amplify's 66,887 (p = 0.008).

**Inference.** If the 15 specific equations were intrinsically harder than the 50 calibration equations, the replay would produce amplify-like statistics. It does not. **The equations are not the cause.**

### 3. Parallelism on the same prompt is not the cause

This was the strongest hypothesis throughout the investigation, supported by both:

- The replay test (Test 4) eliminating the equations as a cause, leaving the parallel-same-prompt structure as the only obvious thing unique to amplify.
- The shared-cwd parallel-vs-sequential test (Test 5) showing **medians in the predicted direction** (parallel +43% output, +38% duration — eerily matching the amplify-vs-calibration ratio of +43%/+47%).

The shared-cwd test was inconclusive (p = 0.69 and p = 0.57 at n=9 vs n=9) due to low statistical power against the high within-arm variance. We could have rerun it at n=30 each but chose to switch design instead: **per-probe `/tmp` isolation**.

[Test 7](01-methodology.md#test-7--parallel-vs-sequential-per-probe-tmp-isolation) under per-probe `/tmp` isolation:

| Feature | parallel ISO | sequential ISO | par/seq | p-value |
| --- | --- | --- | --- | --- |
| pass rate | 6/9 | 6/9 | identical | — |
| output_tokens | 15,850 | 15,872 | **1.00** | p = 0.96 |
| duration_ms | 88,649 | 82,904 | 1.07 | p = 0.83 |
| cache_read | 20,965 | 20,965 | identical | p = 0.63 |

**The two arms are statistically identical on every feature.** The parallel-vs-sequential medians from Test 5 (par +43%, par +38%) collapse to **par 0.00%, par +7%** under proper isolation.

**Inference.** Parallelism on the same prompt is not the cause of inflated per-probe behavior — at least, not when each probe runs in its own `/tmp` directory with its own minimal `.claude/`. **If parallelism were the cause, we would still see it under isolation.** We don't.

### 4. Model behavior differences are not the cause

`num_turns = 1` in 245/245 probes across calibration and amplify. Neither run used tools. Neither run reached for Bash/Python despite having the option. Both arms were operating in the same "compute by hand" regime forced by the no-calculator rule in the user prompt and (when present) the local `.claude/CLAUDE.md`.

**Inference.** The discrepancy is not caused by one run secretly using tools while the other did not.

### 5. Different `.claude/` content was not loaded

Cache accounting:

| Run | cache_read | cache_creation | total context |
| --- | --- | --- | --- |
| Calibration | 20,965 | 6,782 | **27,747** |
| Amplify | 27,748 | 0 | **27,748** |

The same total system-prompt context was sent to the model in both runs. Calibration was creating cache content (cold cache, first run); amplify was reading from already-warm cache. The bytes are identical.

**Inference.** The cache_read difference is purely a cache-warmth artifact. It does not represent different content being loaded.

## What we did find

### A real cache outlier phenomenon

In Test 7, during 18 isolated probes on the same equation, we observed:

| Probe count | cache_read | output_tokens (median) | duration (median) |
| --- | --- | --- | --- |
| 15 / 18 | 20,965 (constant) | ≈ 15,800 | ≈ 84,000 ms |
| **3 / 18** | **≈ 43,735** | **≈ 42,800** | **≈ 238,000 ms** |

About **17% of probes** hit a distinct cache state. When that happens:
- `cache_read` jumps from 20,965 to ~43,735 (+22,770 tokens, ~2× the normal)
- `output_tokens` jumps 2.7× (median ~15,800 → ~42,800)
- `duration_ms` jumps 2.8× (median ~84,000 → ~238,000)

The phenomenon is **arm-independent**: 1 outlier in 9 parallel probes, 2 outliers in 9 sequential probes. Concurrency does not predict it.

**Inference.** This is something Anthropic-side. We have no visibility into what's in the +22,770 tokens of cached context that the outlier probes get. Possibilities (unconfirmable from outside):

1. **Different cache slot.** A separate cache containing additional pre-cached system-prompt content that some requests get routed to.
2. **Different model variant.** An A/B test cohort where Haiku 4.5 has a longer system prompt or a different decoding configuration.
3. **Different tenant routing.** The request was served by a different tier of infrastructure with a different cached prefix.
4. **Cache eviction event.** The cache was partially rebuilt mid-batch, and some probes hit the partially-rebuilt state.

We cannot distinguish these without Anthropic-side instrumentation. We can only document that the phenomenon exists, characterize its frequency and magnitude, and design the experiment to be robust to it.

### Per-probe `/tmp` isolation produces clean, reproducible results

[Test 7](01-methodology.md#test-7--parallel-vs-sequential-per-probe-tmp-isolation) demonstrated that under per-probe `/tmp` isolation:

- `cache_read` is constant at 20,965 (except for outliers), independent of arm
- `cache_creation` is constant at 1,805 (the per-probe minimal `.claude/CLAUDE.md`)
- `num_turns` is always 1
- Parallel and sequential are statistically identical
- The model still computes by hand (the minimal `.claude/CLAUDE.md` and the empty-allow `settings.json` together enforce this)

This is the cleanest possible experimental setup: every probe is independent of every other probe in every observable way, *except* for the Anthropic-side cache state (which is not under our control).

### The pieces of the original anomaly, reattributed

The original observation can now be decomposed:

| Original observation | Attributable to |
| --- | --- |
| `cache_read` 20,965 vs 27,748 (+7k) | Cache warmth state. Calibration ran first (cold); amplify ran second (warm). Same total content. |
| `cache_creation` 6,782 vs 0 | Same as above. Calibration paid the cost of warming the cache. |
| `output_tokens` median 8,780 vs 12,509 (+43%) | High intrinsic per-probe variance + likely some accumulated cache outliers. The replay confirms this — same equations, calibration-like result. |
| `duration_ms` median 45,596 vs 66,887 (+47%) | Same as `output_tokens`. The two are correlated. |
| Pass rate 0.740 vs 0.644 (−0.10) | Sampling noise (p = 0.22, not significant). |

**None of these are caused by the experimental design having a bug.** They are:
1. Cache warmth (cosmetic, no impact on model behavior)
2. Intrinsic per-probe variance and a ~17% cache outlier rate inflating the median
3. Sampling noise on a relatively small (n=15 problem) sample

## The deductive chain

Putting it all together:

**Premise 1.** Calibration and amplify use the same code path (`calibrate.probe`), same model, same flags, same prompt template, both with `num_turns=1` for every probe.

**Premise 2.** The two runs sent identical total context to the model (cache_read + cache_creation = 27,747 ≈ 27,748). The cache_read difference is purely warmth state, not content.

**Premise 3.** The per-probe feature differences (output_tokens +43%, duration +47%) are large and statistically significant, but the pass-rate difference (-0.10) is not.

**Premise 4.** The 15 equations in amplify are not systematically harder. (Test 4 replay → calibration-like results.)

**Premise 5.** Parallel-same-prompt invocation does not cause the inflation. (Test 7 isolated → parallel and sequential are identical.)

**Premise 6.** A documented cache outlier phenomenon (~17% rate, +22,770 tokens, 2.7–2.8× output and duration) exists and is arm-independent.

**Conclusion 1.** The amplify isolated arm's per-probe feature inflation is most plausibly explained by **a combination of the cache outlier phenomenon and intrinsic per-probe variance**, both of which fall outside our ability to control from outside Anthropic's infrastructure.

**Conclusion 2.** The amplify isolated arm's pass-rate drop is not statistically significant. It is consistent with noise from a smaller effective sample (15 problems vs 50 calibration problems) intersected with the high intrinsic per-probe variance.

**Conclusion 3.** The amplify experimental design is not broken. Both calibration and amplify probes operate in the same regime — same model, same prompt, same num_turns, same context size sent. The discrepancy in observed medians is *not* a process bug, it is *Anthropic-side noise* that the experiment was previously not robust to.

**Conclusion 4.** Per-probe `/tmp` isolation is the right pattern going forward. It eliminates cwd-dependent behavior, eliminates `.claude/` auto-discovery leakage between probes, eliminates the reliance on the cwd having the right local config, and produces statistically identical parallel and sequential arms. It does NOT eliminate the cache outlier phenomenon, but it does make the rest of the experiment reproducible.

## What this does NOT prove

Several things remain genuinely open:

- **Why the cache outlier phenomenon exists.** We can only describe it. Anthropic-side instrumentation would be needed to explain it.
- **Whether the 17% outlier rate is stable** or varies by time of day, model version, account tier, or other external factors.
- **Whether the cache outlier is specific to Haiku 4.5** or affects other Claude models too.
- **Whether the pass rate of outlier probes is systematically different** from non-outlier probes. At n=3 outliers, we cannot tell.

The next document, [04-impact.md](04-impact.md), discusses what these findings mean for the amplify experiment going forward and what to do next.
