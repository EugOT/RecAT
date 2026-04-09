# 02 — Findings

This document presents the data from the diagnostic tests in [01-methodology.md](01-methodology.md). All raw inputs are in [data/](data/).

## Data sources

| File | Source | Contents |
| --- | --- | --- |
| [data/01-calibration-report.json](data/01-calibration-report.json) | `traces/calibrate-20260408-124726/report.json` | Pass 1 calibration run, 110 probes across staircase |
| [data/02-amplify-report.json](data/02-amplify-report.json) | `traces/amplify-20260408-145121/report.json` | M=15 isolated arm (135 probes), non-isolated arm deleted |
| [data/03-replay-results.json](data/03-replay-results.json) | `traces/replay-20260408-174417/results.json` | Replay: 15 equations × 3 reps = 45 probes |
| [data/04-parvseq-shared-cwd-results.json](data/04-parvseq-shared-cwd-results.json) | `traces/parvseq-20260408-175912/results.json` | 9 parallel + 9 sequential, shared cwd |
| [data/05-isoprobe-tmp-isolation-results.json](data/05-isoprobe-tmp-isolation-results.json) | `traces/isoprobe-20260408-182625/results.json` | 9 parallel + 9 sequential, per-probe `/tmp` isolation |

## Headline numbers

### Pass rates across all five datasets

| Dataset | n | k | p̂ | 95% Wilson CI | Notes |
| --- | --- | --- | --- | --- | --- |
| Calibration cell 7 | 50 | 37 | **0.740** | [0.604, 0.841] | Original calibration |
| Amplify isolated (pooled) | 135 | 87 | **0.644** | [0.561, 0.720] | All 135 attempts on 15 problems |
| Replay (same equations) | 45 | 34 | **0.756** | [0.611, 0.860] | 15 equations × 3 reps |
| Parvseq parallel (shared cwd) | 9 | 5 | 0.556 | n=9 too small | One equation, 9 simultaneous |
| Parvseq sequential (shared cwd) | 9 | 5 | 0.556 | n=9 too small | One equation, 9 sequential |
| Isoprobe parallel (`/tmp` iso) | 9 | 6 | **0.667** | n=9 too small | One equation, 9 simultaneous, isolated |
| Isoprobe sequential (`/tmp` iso) | 9 | 6 | **0.667** | n=9 too small | One equation, 9 sequential, isolated |

**Key observations.**

1. The original calibration (0.740) and the replay (0.756) agree closely. The amplify isolated arm (0.644) is the outlier — but only by 0.10, and not statistically significant against calibration (z = -1.23, p = 0.22).
2. The parvseq tests on a single hard equation (0.556) and the isoprobe tests on the same equation (0.667) are lower than the population average. This is expected: the test equation we picked happens to be harder than the cell average. It's the *equality between arms* that matters in those tests, not the absolute level.

### Per-probe feature comparison: calibration vs amplify

Computed from 110 calibration probes vs 135 amplify isolated probes via Mann-Whitney U.

```
         feature     cal med     amp med   replay med   replay vs cal   replay vs amp
----------------  ----------  ----------  -----------  --------------  --------------
   output_tokens        8780       12509        10726  p=0.0648        p=0.0139   *
      cache_read       20965       27748        27748  p=0.0000 ***    p=0.8235
  cache_creation        6782           0            0  p=0.0003 ***    p=0.7662
     duration_ms       45596       66887        54705  p=0.0722        p=0.0082  **
```

(Significance: \* p<0.05, \*\* p<0.01, \*\*\* p<0.001)

**Reading this table:**

- **`output_tokens`**: amplify median is 43% higher than calibration. Replay (same equations as amplify, but no parallel-same-prompt) is significantly different from amplify (p=0.014) and only marginally different from calibration (p=0.065). **The replay matches calibration, not amplify** — equations alone don't explain the gap.
- **`duration_ms`**: same story. Replay matches calibration, both are significantly lower than amplify.
- **`cache_read` and `cache_creation`**: replay matches amplify (both run after calibration, hitting warm cache). Calibration is the outlier here, but only because it ran first when the cache was cold. **Total context size is identical**: cal 20,965+6,782 = 27,747; amp/replay 27,748+0 = 27,748.

**The conclusion from this table:** the cache differences are pure cache-warmth artifact (no content difference). The output_tokens / duration differences are real and unexplained — but the equations themselves don't explain them, because the replay produces calibration-like values on the same equations.

### Test 5: parallel vs sequential, SHARED cwd

| Feature | parallel (n=9) | sequential (n=9) | par/seq | p-value |
| --- | --- | --- | --- | --- |
| **pass rate** | 5/9 = 0.556 | 5/9 = 0.556 | identical | — |
| `output_tokens` median | 18,400 | 12,876 | **+43%** | p = 0.69 |
| `duration_ms` median | 94,277 | 68,150 | **+38%** | p = 0.57 |
| `cache_read` | 27,748 | 27,748 | 1.00 | p = 0.76 |
| `cache_creation` | 0 | 0 | — | p = 0.43 |

The medians point in the predicted direction (parallel +43% on output, +38% on duration — *exactly matching* the original amplify-vs-calibration ratios). But neither is statistically significant at n=9 vs n=9 because within-arm variance is huge:

- Parallel arm output_tokens range: 9,179 → 46,382 (5× spread)
- Parallel arm duration range: 50s → 241s (5× spread)
- Sequential arm output_tokens range: 8,731 → 44,553 (5× spread)
- Sequential arm duration range: 45s → 239s (5× spread)

This was the most ambiguous test in the investigation. The medians said "yes, parallel-same-prompt inflates," but the variance said "we don't have power to confirm." Test 7 resolved the ambiguity.

### Test 7: parallel vs sequential, PER-PROBE `/tmp` ISOLATION

| Feature | parallel ISO (n=9) | sequential ISO (n=9) | par/seq | p-value |
| --- | --- | --- | --- | --- |
| **pass rate** | **6/9 = 0.667** | **6/9 = 0.667** | **identical** | — |
| `output_tokens` median | 15,850 | 15,872 | **1.00** | p = 0.96 |
| `duration_ms` median | 88,649 | 82,904 | 1.07 | p = 0.83 |
| `cache_read` | 20,965 | 20,965 | identical | p = 0.63 |
| `cache_creation` | 1,805 | 1,805 | identical | p = 0.86 |
| `num_turns` | 1 | 1 | identical | p = 1.00 |

**Under per-probe `/tmp` isolation, parallel and sequential are statistically identical on every feature.** The parallel-same-prompt hypothesis is refuted.

Note that `cache_read = 20,965` exactly matches calibration's median, NOT amplify's 27,748. The isolated probe primitive produces calibration-like cache state by construction (each probe has its own minimal `.claude/`, no auto-discovered local context). And cache_creation ≈ 1,805 per probe is the per-probe `.claude/CLAUDE.md` content being newly cached each time.

## The cache outlier phenomenon

While analyzing Test 7, we noticed 3 of 18 probes had a distinct cache state.

### The 3 outliers (out of 18 isolated probes)

| Probe | Arm | Output tokens | Duration | cache_read | cache_creation | Passed |
| --- | --- | --- | --- | --- | --- | --- |
| par-08 | parallel | 42,806 | 238s | **43,733** | 1,844 | ✓ |
| seq-04 | sequential | 41,246 | 224s | **43,735** | 1,846 | ✓ |
| seq-09 | sequential | 51,199 | 280s | **43,735** | 1,846 | ✗ |

### Compared to the other 15 probes

| Statistic | Normal probes (n=15) | Outliers (n=3) | Ratio |
| --- | --- | --- | --- |
| `cache_read` | **20,965** (constant) | **43,735** | **2.09×** |
| `output_tokens` median | ≈ 15,800 | ≈ 42,800 | ≈ **2.7×** |
| `duration_ms` median | ≈ 84,000 | ≈ 238,000 | ≈ **2.8×** |

**Properties of the outlier phenomenon.**

1. **Bimodal cache state.** All non-outlier probes had `cache_read = 20,965` exactly. All outliers had `cache_read ≈ 43,735` (very close, but slightly different — 43,733 vs 43,735). The difference is +22,770 tokens, slightly more than double.
2. **Arm-independent.** 1 outlier in 9 parallel probes, 2 outliers in 9 sequential probes. The 17% rate is consistent and concurrency does not predict outliers.
3. **Strong correlation with output and duration.** When a probe hits the outlier cache state, its output_tokens and duration jump 2.7–2.8× alongside.
4. **Pass rate impact unclear.** Of 3 outliers, 2 passed and 1 failed. Sample too small to conclude pass-rate effect.
5. **Small `cache_creation` is similar in both** (~1,805 vs ~1,845) — the per-probe `.claude/CLAUDE.md` is being newly cached either way. The difference is in what's *read* from cache, not what's written.

### What we know about the +22,770 tokens

The `+22,770` cache_read tokens that distinguish the outlier from the normal probes are not in our control:

- We did not change the local `.claude/` between probes (each probe writes its own freshly into a new `tempfile.TemporaryDirectory()`).
- The `cache_creation` is essentially the same (~1,805) in both, so we are not creating different content.
- The model is the same in both (`claude-haiku-4-5-20251001`).
- The prompt is the same in both (`calibrate.PROMPT_TEMPLATE` formatted with the same equation).
- The flags are the same in both.

The 22,770 extra cache_read tokens come from **somewhere inside Anthropic's serving infrastructure**. We have no way to inspect what they are. Possibilities (none confirmable from outside):

- A different cache slot containing additional pre-cached system context loaded by the routing layer
- A different model variant or A/B test cohort with a longer system prompt
- A different tenant cache tier (e.g., a larger shared cache that includes extra context)
- A cache eviction event causing the cache to be partially rebuilt with different content

We can characterize the phenomenon statistically (frequency ≈ 17%, magnitude ≈ +22.7k cache tokens, output and duration jump 2.7–2.8×) but cannot explain the mechanism without Anthropic-side visibility.

## Process verification

### Subprocess `cwd` override actually works

To rule out the possibility that `subprocess.run(cmd, cwd=str(tmp_path))` was not actually changing the child process's cwd, we ran a verification:

```python
with tempfile.TemporaryDirectory(prefix="cwdcheck-") as tmp:
    tmp_path = Path(tmp)
    # write minimal .claude/ with Bash temporarily allowed
    ...
    subprocess.run(["claude", "--print",
                    "Run `pwd` and `ls -la .claude/`. Report exactly what they printed.",
                    ...],
                   cwd=str(tmp_path), ...)
```

Child output:
```
/private/var/folders/pt/jnjtyxx578bfv5fpncwr_vqm0000gn/T/cwdcheck-yc828pi7

total 16
drwxr-xr-x  4 adamsohn  staff  128 Apr  8 18:52 .
drwx------  4 adamsohn  staff  128 Apr  8 18:52 ..
-rw-r--r--  1 adamsohn  staff   11 Apr  8 18:52 CLAUDE.md
-rw-r--r--  1 adamsohn  staff   77 Apr  8 18:52 settings.json
```

**Confirmed:** the child's cwd is the temp directory, and `ls .claude/` shows only the two files we wrote there. The `applications/amplify/.claude/` files are not visible to the child. Per-probe isolation is real and tight.

### Both calibration and amplify probes had `num_turns = 1` (no tool use)

Cross-checked across all 245 probes:

| Run | num_turns distribution |
| --- | --- |
| Calibration (n=110) | `{1: 110}` (all probes) |
| Amplify isolated (n=135) | `{1: 135}` (all probes) |

So the question "did calibration secretly use Python while amplify did not?" has a definitive answer: no. Both runs had every probe stay at exactly one assistant turn. Neither used tools.

### Total system-prompt context size in calibration vs amplify is the same

Cache accounting math:

| Run | cache_read (median) | cache_creation (median) | sum |
| --- | --- | --- | --- |
| Calibration | 20,965 | 6,782 | **27,747** |
| Amplify | 27,748 | 0 | **27,748** |

Identical total context. The cache_read difference of 6,783 between the two runs is matched almost exactly by the cache_creation difference of 6,782. Calibration was creating new cache content; amplify was reading from already-warm cache. **The two runs sent the same content to the model**, just with different cache hit rates.

This rules out "different `.claude/` content was loaded" as a hypothesis. The content was identical; only the cache state differed.

The next document, [03-conclusions.md](03-conclusions.md), interprets these findings.
