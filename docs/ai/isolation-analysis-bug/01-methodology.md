# 01 — Methodology

This document records every diagnostic test we ran, in chronological order, with the question each one was designed to answer and the result.

The investigation followed a hypothesis-elimination structure: start broad, identify a specific mechanism that *could* explain the anomaly, design a test that would either confirm or refute it, run the test, update the hypothesis space.

## Tests, in order

### Test 1 — Pooled per-attempt comparison

**Question.** Is the calibration-vs-amplify difference statistically significant at the per-attempt level?

**Method.** Pool all 135 attempts in the amplify isolated arm (15 problems × 9 attempts each) into a single Bernoulli sample. Compare against calibration's 50 probes with a two-proportion z-test.

**Result.**
- calibration: 37/50 = 0.740, 95% CI [0.604, 0.841]
- amplify pooled: 87/135 = 0.644, 95% CI [0.561, 0.720]
- z = -1.23, p = 0.22 — **not significant**

**Conclusion.** The pass-rate difference alone is consistent with sampling noise. But this does not address the highly significant per-probe feature differences (output_tokens, duration). Continue investigation.

---

### Test 2 — Per-feature Mann-Whitney U comparison

**Question.** Beyond pass rate, are calibration and amplify probes statistically different on any per-probe feature extracted from the `result` event of the stream-json output?

**Method.** Parse the `result` event from every conversation log in both runs. Pull `output_tokens`, `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `duration_ms`, `num_turns`, `cost_usd`. Run Mann-Whitney U on each feature pair.

**Result.** See [00-problem.md](00-problem.md) for the full table. Summary:
- `output_tokens`: amplify 43% higher, **p < 0.0001**
- `duration_ms`: amplify 47% longer, **p < 0.0001**
- `cache_read`: amplify 32% higher, **p < 0.0001**
- `cache_creation`: amplify median = 0, calibration median = 6,782, **p < 0.0001**
- `num_turns`: identical (1 in both, all 245 probes)
- `input_tokens`: identical (10 tokens, the user prompt + minimal framing)

**Conclusion.** Multiple features are highly significantly different. Same model, same flags, same prompt — but the per-probe work is substantially heavier in amplify. The cache_read + cache_creation totals approximately match (calibration: 27,747; amplify: 27,748), suggesting the *total* system-prompt context size is the same and the difference is purely cache-warmth state. But the inflated `output_tokens` and `duration_ms` are unexplained.

---

### Test 3 — `cwd` discovery test

**Question.** Does the cwd at probe-launch time affect what gets loaded as system-prompt context?

**Method.** Spawn a single fresh `claude --print` with the same equation from three different cwds:
- `/tmp` (no `.claude/` discoverable)
- repo root `/Users/adamsohn/Projects/agent-specification` (only the empty repo-root `.claude/CLAUDE.md`)
- `applications/amplify/` (full local `.claude/` with CLAUDE.md + rules + settings)

**Result.**

| cwd | cache_read | output_tokens | duration | num_turns |
| --- | --- | --- | --- | --- |
| `/tmp` | 50,340 | 298 | (fast) | **2** |
| repo root | 45,148 | 552 | (fast) | **2** |
| `applications/amplify/` | 27,513 | 5,031 | (slower) | **1** |

**Conclusion.** The cwd matters substantially. Without the local `applications/amplify/.claude/`, the model gets `num_turns=2` and writes only 298–552 output tokens — meaning it **delegates the math to a tool** (Python via Bash) and gives back a brief answer. With the local `.claude/`, the model gets `num_turns=1` and writes 5,031 output tokens — it **computes by hand**.

So the local `.claude/CLAUDE.md` and `rules/arithmetic.md` are **load-bearing for the experimental contract**: they enforce the no-calculator rule. Without them, the experiment is silently broken (the model just uses Python and gets ~100% trivially).

This also explains why both calibration and amplify had `num_turns=1` for all 245 probes: both runs were launched from a cwd where the local `.claude/` was discoverable (specifically `applications/amplify/`).

But this test does NOT explain the inflated output tokens / duration in amplify. Both calibration and amplify load the same `.claude/` files. Continue investigation.

---

### Test 4 — Replay (controlled equation comparison)

**Question.** Are amplify's 15 specific equations systematically harder than calibration's 50? In other words, does running the same equations fresh produce calibration-like or amplify-like statistics?

**Method.** Extract the 15 expressions from `amplify-20260408-145121/report.json`. Run a fresh probe on each, 3 reps each (45 total invocations) via the same `calibrate.probe()` code path, parallel=5, but with **different equations** in each parallel batch (matching calibration's batching pattern, NOT amplify's same-equation pattern).

This isolates a single variable: are the equations the cause?

**Script.** `runner/calibrate_replay.py` (deleted after investigation). Raw output saved to `data/03-replay-results.json`.

**Result.** Compared replay against both calibration and amplify on the same features:

| Feature | calibration median | amplify median | replay median | replay vs cal | replay vs amp |
| --- | --- | --- | --- | --- | --- |
| **pass rate** | 0.740 | 0.644 | **0.756** | matches cal | higher than amp |
| `output_tokens` | 8,780 | 12,509 | 10,726 | p = 0.065 | **p = 0.014 *** |
| `duration_ms` | 45,596 | 66,887 | 54,705 | p = 0.072 | **p = 0.008 ** |
| `cache_read` | 20,965 | 27,748 | 27,748 | p < 0.001 | matches amp (warm cache) |
| `cache_creation` | 6,782 | 0 | 0 | p < 0.001 | matches amp |

**Conclusion.** **The equations are not the cause.** The replay used the exact same 15 equations and produced a **0.756 pass rate** (matching calibration's 0.74, NOT amplify's 0.64). Replay's `output_tokens` and `duration_ms` are significantly LOWER than amplify's even on the same equations. The cache state in replay matches amplify (post-calibration warm cache), confirming the cache_read/creation difference between calibration and amplify is purely a cache-warmth artifact (calibration ran first when cache was cold; everything since has been warm), not a content difference.

What's left as a candidate for the amplify-specific inflation: the **structural pattern** of how amplify runs probes that nothing else does. Specifically, amplify's isolated arm fires **5–9 parallel `claude --print` requests on the EXACT SAME PROMPT** simultaneously. Replay used parallel=5 too, but on different equations per batch. Calibration used parallel=5 on different equations per batch. Only amplify did parallel-same-prompt.

This was the next hypothesis: **parallel-same-prompt invocation causes per-probe behavior changes**.

---

### Test 5 — Parallel vs sequential, shared cwd

**Question.** Does firing N identical `claude --print` requests in parallel produce different per-probe behavior than firing N sequential ones, on the same equation?

**Method.** Pick one equation (the first from the amplify report). Run two arms:
- **PARALLEL (n=9):** fire 9 simultaneous `claude --print` via `ThreadPoolExecutor(max_workers=9)`
- **SEQUENTIAL (n=9):** fire 9 one at a time

Same equation, same model, same flags, same cwd (`applications/amplify/`). Compare per-feature distributions via Mann-Whitney U.

**Script.** `runner/parallel_vs_sequential.py` (deleted after investigation). Raw output saved to `data/04-parvseq-shared-cwd-results.json`.

**Result.**

| Feature | parallel (n=9) | sequential (n=9) | par/seq | p-value |
| --- | --- | --- | --- | --- |
| **pass rate** | 5/9 = 0.556 | 5/9 = 0.556 | identical | — |
| `output_tokens` median | 18,400 | 12,876 | **+43%** | p = 0.69 |
| `duration_ms` median | 94,277 | 68,150 | **+38%** | p = 0.57 |
| `cache_read` | 27,748 | 27,748 | 1.00 | p = 0.76 |
| `cache_creation` | 0 | 0 | — | p = 0.43 |

**Conclusion — interpretively complicated.** The medians point in the predicted direction: parallel arm has **+43% output tokens** and **+38% duration** vs sequential — eerily similar to the original amplify-vs-calibration ratio of +43%/+47%. **But neither difference is statistically significant** at n=9 vs n=9, because the within-arm variance is enormous (sequential range: 8,731 → 44,553 output tokens on the *same equation*).

Crucially, the **sequential arm itself shows wide per-probe variance** on identical input, which means the model has high intrinsic variance in how much work it does on a given problem regardless of concurrency. This complicates the original interpretation: maybe the calibration-vs-amplify gap is just sampling noise within a high-variance distribution.

We still didn't know whether parallel-same-prompt was the cause or whether the test simply had insufficient statistical power. Two ways forward: (a) run it with much higher reps (~30 each = ~$6, ~45 min), or (b) try a different angle. We chose (b): switch to per-probe `/tmp` isolation, modeled on the orchestrator pattern.

---

### Test 6 — `/tmp` isolation primitive verification

**Question.** Can we cleanly isolate each probe in its own `/tmp` directory such that no two probes share any filesystem state? And does the isolation work — does the child claude process actually run from `/tmp/...` and not see anything from `applications/amplify/.claude/`?

**Method.**

Phase 1 (build): Modeled on `applications/orchestrator/runner/loop.py:invoke_claude_isolated()`. For each probe:

1. `tempfile.TemporaryDirectory(prefix="amplify-probe-")` creates a fresh `/tmp/amplify-probe-XXXX/`
2. Write `<tmp>/.claude/CLAUDE.md` with a minimal "compute by hand" instruction
3. Write `<tmp>/.claude/settings.json` with minimal project-local settings (`autoMemoryEnabled: false`, empty hooks)
4. `subprocess.run([..."claude", "--print", "--tools", "", "--strict-mcp-config", ...], cwd=str(tmp_path))`
5. `tempfile.TemporaryDirectory` auto-cleans on context exit

2026-06-12 correction: the earlier version of step 3 incorrectly claimed that an empty `permissions.allow` list denies all tools. Tool denial must be enforced by CLI flags and verified from the emitted system-init metadata.

Phase 2 (verify): Spawn one claude in a temp dir with Bash temporarily allowed, ask it to print `pwd` and `ls .claude/`, confirm what it reports.

**Script.** `runner/probe_isolated_test.py` for the primitive. The verification was a standalone Python snippet (not saved).

**Result of verification.** The child claude process reported:
- `pwd` → `/private/var/folders/.../cwdcheck-yc828pi7` (the tempdir, NOT `applications/amplify/`)
- `ls .claude/` → only the two files we wrote (`CLAUDE.md`, `settings.json`)

**Conclusion.** Per-probe `/tmp` isolation works. The child process truly runs from the temp dir, can't see `applications/amplify/.claude/`, and has only the minimal context we explicitly write.

---

### Test 7 — Parallel vs sequential, per-probe `/tmp` isolation

**Question.** With the leakage from `applications/amplify/.claude/` and any other shared cwd state eliminated, does the parallel-vs-sequential difference still exist?

**Method.** Same as Test 5 but with each probe wrapped in `tempfile.TemporaryDirectory()`. 9 parallel + 9 sequential, same equation, same model, same flags. Compare features.

**Script.** `runner/probe_isolated_test.py`. Raw output saved to `data/05-isoprobe-tmp-isolation-results.json`.

**Result.**

| Feature | parallel ISO (n=9) | sequential ISO (n=9) | par/seq | p-value |
| --- | --- | --- | --- | --- |
| **pass rate** | **6/9 = 0.667** | **6/9 = 0.667** | **identical** | — |
| `output_tokens` median | 15,850 | 15,872 | **1.00** | p = 0.96 |
| `duration_ms` median | 88,649 | 82,904 | 1.07 | p = 0.83 |
| `cache_read` | 20,965 | 20,965 | identical | p = 0.63 |
| `cache_creation` | 1,805 | 1,805 | identical | p = 0.86 |
| `num_turns` | 1 | 1 | identical | p = 1.00 |

**Conclusion — definitive.** Under per-probe `/tmp` isolation, parallel and sequential are **statistically indistinguishable on every feature**. The parallel-same-prompt hypothesis is **refuted**.

But during this test we noticed something else: 3 of 18 probes (≈17%) hit a distinct anomaly with `cache_read ≈ 43,735` (vs the normal 20,965), output tokens 41,246–51,199 (3–4× higher), and duration 224–280 seconds. **This anomaly appeared in BOTH arms** (par-08 in parallel, seq-04 and seq-09 in sequential). It is **not concurrency-related**.

That's the cache outlier phenomenon. We characterize but cannot explain it.

---

## Summary of methodology

| # | Test | Question | Result |
| --- | --- | --- | --- |
| 1 | Pooled comparison | Is the pass-rate gap significant? | No (p=0.22) |
| 2 | Mann-Whitney U on features | Are per-probe features significantly different? | Yes (output_tokens, duration, cache state all p<0.0001) |
| 3 | cwd discovery | Does cwd matter? | Yes — local `.claude/` enforces no-calc rule, was loaded in both calibration and amplify |
| 4 | Replay | Are the equations the cause? | **No** (replay on same equations → calibration-like results) |
| 5 | Parallel vs sequential, shared cwd | Is parallelism the cause? | Inconclusive (medians match hypothesis but n=9 underpowered) |
| 6 | `/tmp` isolation primitive | Does the isolation work? | Yes (child cwd is tempdir, no leakage) |
| 7 | Parallel vs sequential, isolated | Is parallelism the cause under isolation? | **No** (par and seq statistically identical) |

The sequence eliminated equations as a cause (Test 4), eliminated parallelism as a cause (Test 7), and identified the cache outlier as a real but arm-independent phenomenon (incidentally during Test 7).

The next document, [02-findings.md](02-findings.md), presents the data from these tests in more detail.
