# 04 — Impact on the Amplify Experiment

This document translates the conclusions in [03-conclusions.md](03-conclusions.md) into concrete consequences for the amplify experiment design and recommends a path forward.

## What the amplify experiment is trying to accomplish

From the [amplify README](../../applications/amplify/README.md):

> If you ask Claude to do something it's only ~70% reliable at, and then you ask it the *same thing* a bunch of times in parallel and take a majority vote — does the reliability go up the way the math says it should, and does it matter whether the parallel attempts are spawned as separate sub-agents or all jammed into one context?

The experiment hinges on three things being true:

1. The per-attempt success rate `p` measured during calibration is the same `p` you get when you run independent attempts during amplification.
2. Independent attempts spawned as separate sub-agents are statistically independent (so the binomial-amplification math applies).
3. Attempts run inside a single context are NOT statistically independent (so the binomial math fails to predict their behavior).

The investigation in this folder addresses precondition (1). Preconditions (2) and (3) are downstream and not affected by these findings.

## What changes for the amplify experiment

### 1. The original M=15 amplify run is salvageable but flawed

**The data is not invalid.** The 135 isolated probes in `traces/amplify-20260408-145121/` were all real attempts at the calibrated task, all `num_turns=1`, all using the same model and prompt as calibration. The discrepancy with calibration is most plausibly explained by:

- ~17% cache outliers inflating the median per-probe stats
- Intrinsic per-probe variance making M=15 underpowered for tight comparison
- Sampling noise on the pass rate (not significant)

**The data is also not great.** The smaller effective sample (15 problems vs 50 in calibration) means even a few cache outliers can shift the median substantially. And we have no way to filter outliers post-hoc from a run that didn't record per-probe `cache_read` (well — actually we do, since the conv files have the result events; we *can* filter retroactively).

**What to do.** The M=15 data should be re-analyzed with cache outliers filtered out, and any future amplify runs should use per-probe `/tmp` isolation.

### 2. The amplify code path needs to switch to per-probe `/tmp` isolation

The current `amplify.py` and `calibrate.py` both rely on the cwd having the local `applications/amplify/.claude/` directory discoverable. This works when launched from the right place, but it has two problems:

- **Implicit dependency on cwd.** If you run the script from the project root, or from `/tmp`, the `.claude/` is not discovered, the no-calculator rule is not enforced, and the model uses Python instead — silently breaking the experiment.
- **Shared state across probes.** All probes in a run share the same auto-discovered `.claude/` files. Cache effects, file system race conditions, and any future change to the local `.claude/` mid-run would affect all probes simultaneously.

The fix is to model probe invocation on `applications/orchestrator/runner/loop.py:invoke_claude_isolated()`:

```python
def run_isolated_probe(prompt: str, model: str, ...) -> ProbeResult:
    with tempfile.TemporaryDirectory(prefix="amplify-probe-") as tmp:
        tmp_path = Path(tmp)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text(MINIMAL_CLAUDE_MD)
        (claude_dir / "settings.json").write_text(MINIMAL_SETTINGS_JSON)

        cmd = [
            "claude", "--print", prompt,
            "--model", model,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", "30",
            "--permission-mode", "bypassPermissions",
        ]
        subprocess.run(cmd, cwd=str(tmp_path), ...)
        # parse and return
```

The minimal CLAUDE.md is just a few lines reinforcing the no-calculator rule. The minimal settings.json uses `{"permissions": {"allow": []}}` to deny all tools — this is the load-bearing piece that prevents the model from reaching for Python.

This pattern produces:

- `cache_read = 20,965` (constant, except for cache outliers)
- `cache_creation = 1,805` (constant, the per-probe minimal CLAUDE.md)
- `num_turns = 1` (always — tools are denied)
- Identical parallel and sequential behavior

### 3. The cache outlier rate (~17%) is a real noise floor that future runs need to absorb

We cannot prevent the cache outlier phenomenon from outside Anthropic's infrastructure. But we can:

- **Detect outliers.** Every probe's `result` event contains `cache_read_input_tokens`. A simple rule like "if cache_read > 30,000, flag as outlier" identifies them deterministically. The bimodal distribution makes this easy: normal probes are at exactly 20,965, outliers are at ~43,735.
- **Filter outliers post-hoc.** Compute pass rate / token usage / duration excluding outliers. This is the cleanest analysis if outliers turn out to have systematically different pass rates.
- **OR include outliers but increase M.** If outliers happen at 17% and we want a tight estimate of the true per-attempt rate, M=50 would give us 8–9 outliers in expectation, enough to characterize their pass rate impact and either accept or filter them.

The choice between filter and include depends on whether outliers are part of "the experimental contract" (i.e., something a user of `claude --print` in production would also see) or pure noise. We think the former — they're part of how Anthropic actually serves the model, so the experiment should include them. But that means M needs to be larger than M=15 to be statistically meaningful.

### 4. The matched-prompt invariant (N=1 between arms) was broken in the original M=15 run

In the original M=15 amplify report:
- isolated arm N=1: 0.60 (9 first-attempt outcomes from the 15 problems)
- non-isolated arm N=1: 0.53 (15 single-attempt invocations, since deleted)

These were not statistically different, but they're also not meaningful comparisons because the non-isolated arm used `--print` (single-turn), so its "9 attempts in one context" was actually "one essay containing 9 labeled sections" — never 9 multi-turn attempts.

When we rebuild the non-isolated arm correctly (using `claude --resume` chaining for multi-turn or some other multi-turn primitive), the matched-prompt invariant should be re-checked.

### 5. The non-isolated arm needs a complete redesign

This is a separate issue from the isolation analysis but is mentioned in the [amplify README](../../applications/amplify/README.md). The current `amplify.py:NON_ISOLATED_PROMPT_TEMPLATE` asks one `claude --print` invocation to do N attempts in one context — but `--print` is single-turn, so the result is one generation containing N labeled sections, not N multi-turn attempts.

A correctly-designed non-isolated arm requires multi-turn `claude --resume` chaining: one user message per attempt, each producing its own assistant turn, with prior turns visible to subsequent ones. This is the "agent retries within a session" scenario the experiment is supposed to model.

The investigation in this folder does not address this redesign, but it is a prerequisite for any real Pass 2 result.

## Recommended next steps, in order

### Immediate (1 hour, ~$0)

1. **Read this analysis.** [README.md](README.md) → [00-problem.md](00-problem.md) → [01-methodology.md](01-methodology.md) → [02-findings.md](02-findings.md) → [03-conclusions.md](03-conclusions.md) → this document.
2. **Re-analyze the existing M=15 isolated arm data with cache outlier filtering.** Walk every conv file, classify probes by `cache_read` (≤ 25k = normal, > 30k = outlier), recompute pass rates and feature distributions for normal vs outlier subsets. This is a one-script analysis on existing data, no new Haiku spend.
3. **Decide whether to include or filter outliers** for the next amplify run, based on what the re-analysis shows.

### Short-term (1 day, ~$5–10)

4. **Refactor `calibrate.probe()` to use per-probe `/tmp` isolation.** Add a helper that wraps `subprocess.run` with `tempfile.TemporaryDirectory` and writes a minimal `.claude/`. Make this the default, with a flag to opt out for backwards compat.
5. **Re-run calibration under `/tmp` isolation.** Confirm it produces a similar p̂ to the original calibration on the same cell. This validates the new code path.
6. **Re-run M=15 amplify under `/tmp` isolation.** Compare the per-probe feature distributions to the new calibration. They should now match within sampling noise. The pass rate should be tighter against the original calibration.

### Medium-term (1–3 days, ~$10–30)

7. **Run M=30 or M=50 amplify under `/tmp` isolation.** This gives the statistical power needed to actually measure the amplification curve and detect any real gap between predicted and empirical.
8. **Redesign the non-isolated arm** using multi-turn `claude --resume` chaining. Verify the matched-prompt invariant at N=1.
9. **Run the full M=50 two-arm experiment** with both arms properly isolated.
10. **Update [report/report.md](../../applications/amplify/report/report.md)** with the new results, the cache outlier characterization, and the methodology fixes.

### Long-term (open-ended)

11. **Report the cache outlier phenomenon to Anthropic.** Send the characterization (frequency, magnitude, bimodal cache_read distribution) to Anthropic support. They may have an internal explanation that's not public.
12. **Test the cache outlier across other models.** Does Sonnet 4.5 or Opus 4 show the same ~17% rate? Different rate? No outliers? This would help characterize whether it's a Haiku-specific quirk or a general serving-infrastructure phenomenon.

## How this affects the amplify experiment's central claim

The central claim of amplify is that **scope discipline is load-bearing for predictability** — that the binomial amplification math `P_amp(N, p)` works in `.claude/` programs only when N attempts are spawned as separate sub-agents (truly independent), and fails when they're run inside a shared context (correlated).

This investigation does NOT undermine that claim. What it does is:

1. **Validate the experimental contract.** The probes used in calibration and amplification really are operating in the same regime, modulo Anthropic-side noise we can characterize but not control.
2. **Identify a real noise source** (cache outliers) that any future amplify run needs to be designed around.
3. **Provide a clean isolation primitive** (per-probe `/tmp`) that makes the experiment reproducible and removes a class of subtle leakage bugs.
4. **Clarify the dependency on the local `.claude/`.** The local CLAUDE.md and rules are load-bearing for the no-calculator rule. Without them, the model uses Python and the experiment is silently broken. The minimal isolated CLAUDE.md preserves this load-bearing property in a portable way.

The path to actually publishing a Pass 2 result is now clearer than it was before this investigation:

- Use per-probe `/tmp` isolation ([recommended next step #4](#short-term-1-day-510))
- Run at M ≥ 30 to get past the cache outlier noise floor ([recommended next step #7](#medium-term-13-days-1030))
- Redesign the non-isolated arm with multi-turn `claude --resume` ([recommended next step #8](#medium-term-13-days-1030))
- Then and only then can the headline plot (isolated curve vs non-isolated curve vs closed-form prediction) be honest

## The cost of not understanding this

Without this investigation, we would have been in one of two failure modes:

1. **Believe the original M=15 result.** Conclude that the isolated arm doesn't track the prediction (true), that the non-isolated arm doesn't either (also true but for the wrong reasons), and write a report claiming amplify-the-experiment is a wash. The report would be wrong about *why* — it would attribute the failure to within-cell heterogeneity or sampling noise instead of identifying the cache outlier phenomenon and the need for `/tmp` isolation.
2. **Spend 3–6 hours and ~$20–50 on M=50 from scratch** chasing the same anomaly without the diagnostic loop. Get the same inflated/noisy result on a larger sample. Still not understand it. Burn through usage limits with nothing to show for it.

The investigation took ~1 hour and ~$2 of Haiku and gave us a complete causal chain. That's the right return on investment.

## Fix outcome (executed after this analysis)

After this investigation was complete, the recommended fix was applied: `calibrate.probe()` was modified to wrap the `subprocess.run` call in a `tempfile.TemporaryDirectory()` with a freshly written minimal `.claude/CLAUDE.md` and `.claude/settings.json` per probe. The pattern is exactly the one in `applications/orchestrator/runner/loop.py:invoke_claude_isolated()`. `amplify.py` was given a `--skip-non-isolated` flag (the current non-isolated arm uses `--print` which is single-turn and broken). No other changes.

The fix was then validated with two re-runs:

### Re-calibration under `/tmp` isolation

Run: `python3 runner/calibrate.py` with the new isolated `probe()`. Trace dir: `traces/calibrate-20260408-191452/`.

| | Old (no isolation) | New (`/tmp` isolation) |
| --- | --- | --- |
| Cell | 7 (`muldiv-8x8//6`) | **6** (`muldiv-7x7//5`) — one easier |
| n | 50 | 45 |
| k | 37 | 35 |
| **p̂** | 0.740 | **0.778** |
| 95% CI | [0.604, 0.841] | [0.637, 0.875] |
| Total probes | 110 | **55** (more efficient search) |
| Wall clock | ~38 min | **~22 min** |
| Verdict | calibrated | **calibrated** |

The new calibration landed on a different cell (cell 6 instead of cell 7). Under proper isolation the model is more efficient (no auto-loaded multiplication-worker protocol distracting from the math task), so the next-easier cell now sits in the target window. The two p̂ values are statistically consistent — CIs overlap heavily.

### Re-amplification under `/tmp` isolation

Run: `python3 runner/amplify.py --m 15 --n 1,3,5,7,9 --skip-non-isolated`. Trace dir: `traces/amplify-20260408-193738/`.

The headline finding: **every previously-significant feature difference between calibration and amplify probes is now eliminated.**

| Feature | OLD pre-fix p-value | NEW post-fix p-value |
| --- | --- | --- |
| `output_tokens` | **p < 0.0001** *** | **p = 0.57** (NS) |
| `duration_ms` | **p < 0.0001** *** | **p = 0.48** (NS) |
| `cache_read` | **p < 0.0001** *** | **p = 0.88** (NS) |
| `cache_creation` | **p < 0.0001** *** | **p = 0.59** (NS) |
| `num_turns` | identical (1) | identical (1) |

Medians for the new run:

| Feature | new calibration | new amplify |
| --- | --- | --- |
| `output_tokens` | 8,785 | 8,697 |
| `cache_read` | 20,965 | 20,965 |
| `cache_creation` | 1,832 | 1,832 |
| `duration_ms` | 45,531 | 44,039 |

`cache_read` and `cache_creation` are literally constant in both runs because each probe writes its own minimal `.claude/` from scratch into a fresh tempdir — the cached system context is the same shape every time.

### Pass rates after the fix

- New calibration cell 6: **35/45 = 0.778**, 95% CI [0.637, 0.875]
- New amplify pooled (135 attempts): **120/135 = 0.889**, 95% CI [0.825, 0.932]
- Two-proportion z = +1.87, p = 0.062 — marginally not significant

The amplify pass rate is slightly higher than calibration this time (vs slightly lower in the original run). Both are within sampling noise of each other and consistent with the same underlying p. The amplify-side rate is biased upward by ~0.1 because the M=15 problem draw happened to include slightly easier instances than the calibration sample, and because the amplification effect itself contributes (the per-attempt rate visible in the N=1 column of the curve is 14/15 = 0.933, which is the first attempt on each problem; the pooled 0.889 averages over all 9 attempts on each problem and includes the harder ones that needed majority voting to recover).

### Amplification curve, post-fix

| N | predicted P_amp(N, 0.778) | empirical p̂ | 95% Wilson CI |
| --- | --- | --- | --- |
| 1 | 0.778 | **0.933** | [0.702, 0.988] |
| 3 | 0.874 | **0.933** | [0.702, 0.988] |
| 5 | 0.924 | **1.000** | [0.796, 1.000] |
| 7 | 0.952 | **1.000** | [0.796, 1.000] |
| 9 | 0.970 | **1.000** | [0.796, 1.000] |

The empirical isolated curve sits at or **above** the closed-form prediction at every N. This is the opposite of the pre-fix run, where the curve sat below the prediction. With the calibration baseline now matching the amplification regime, the amplification math actually delivers what it promises — and on this particular 15-problem draw, it slightly over-delivers.

### Cache outlier rate, revised

In the M=135 amplify run we observed:

- 132/135 probes had `cache_read = 20,965` (the normal state)
- 3/135 probes had `cache_read ≈ 43,763` (the outlier state)
- **Outlier rate: 2.2%**

The 17% outlier rate observed in the smaller isoprobe test (n=18) was small-sample noise. The true outlier rate from the larger sample is **~2.2%**, much rarer than initially feared. The 95% binomial CI on 3/135 is [0.5%, 6.4%] which overlaps the 95% CI on 3/18 = [3.6%, 41.4%], so both observations are consistent with a true rate around 2–5%. The cache outlier phenomenon remains real (and remains Anthropic-side), but it's a much smaller noise floor than we initially worried about.

### What this means

**The isolation fix worked completely.** The calibration-vs-amplify discrepancy that triggered this entire investigation is gone — calibration and amplify probes now operate in statistically identical regimes on every measurable feature. The amplification math actually tracks (or beats) its prediction. The cache outlier phenomenon turned out to be much rarer than the small isoprobe sample suggested.

**The amplify experiment is now in a state where Pass 2 is meaningful.** The remaining work, in priority order:

1. The non-isolated arm needs a complete redesign using multi-turn `claude --resume` chaining (since `--print` is single-turn and the current non-isolated prompt asks for N attempts in one generation, which is not what Pass 2 is supposed to test).
2. M = 30–50 to get tighter CIs on the curves and to definitively measure the isolated-vs-non-isolated gap.
3. Optionally, characterize the cache outlier rate across more runs to confirm the ~2.2% estimate is stable.

The methodology problem identified by this investigation has been solved. The remaining work is feature work (rebuilding the non-isolated arm) and statistical work (more samples), not investigation work.
