# 00 — The Problem

## Context

The [amplify](../../applications/amplify/README.md) experiment is built on a precondition: that for a calibrated task class and model pair, the per-attempt success rate `p` measured during calibration (Pass 1) is the same `p` you get when you run independent attempts during the amplification arm (Pass 2). Without that, the binomial-amplification math `P_amp(N, p) = sum_i C(N,i) p^i (1-p)^{N-i}` has nothing to amplify and the predicted curve becomes meaningless.

The two passes are designed to use the **same code path** for the per-attempt invocation: `amplify.py`'s isolated arm directly calls `calibrate.probe()`, which spawns one fresh `claude --print` subprocess per attempt with a fixed set of CLI flags. By construction, an isolated-arm probe at N=1 should be statistically indistinguishable from a calibration probe.

## The observed anomaly

After a successful Pass 1 calibration on Haiku 4.5 — cell `muldiv-8x8//6`, n=50, k=37, **p̂ = 0.740**, 95% Wilson CI [0.604, 0.841] — we ran an M=15 amplification with N ∈ {1, 3, 5, 7, 9}. The isolated arm produced 135 invocations (15 problems × 9 attempts each). Pooled per-attempt:

- **k=87, n=135, p̂ = 0.644**, 95% Wilson CI [0.561, 0.720]

That is **0.10 absolute lower** than the calibration's per-attempt rate. The two-proportion z-test gives z = -1.23, p = 0.22 — not statistically significant. So the pass-rate gap could be sampling noise.

But the model's per-probe behavior was emphatically NOT the same. Comparing 110 calibration probes against 135 amplify isolated probes via Mann-Whitney U on every numeric feature:

| Feature | Calibration median | Amplify median | Ratio | p-value |
| --- | --- | --- | --- | --- |
| `output_tokens` | 8,780 | **12,509** | **+43%** | **p < 0.0001** *** |
| `duration_ms` | 45,596 | **66,887** | **+47%** | **p < 0.0001** *** |
| `cache_read_input_tokens` | 20,965 | 27,748 | +32% | p < 0.0001 *** |
| `cache_creation_input_tokens` | 6,782 | 0 | — | p < 0.0001 *** |
| `input_tokens` | 10 | 10 | identical | p = 0.71 |
| `num_turns` | 1 | 1 | identical | p = 1.00 |
| `cost_usd` | (small) | (larger) | — | p = 0.0003 *** |

The amplify probes were doing **substantially more work per probe** than the calibration probes — writing 43% more output tokens, taking 47% longer — even though the prompt was identical, the model was identical (`claude-haiku-4-5-20251001`), the flags were identical, and the number of turns was identical (1 in every probe of both runs). And the pass rate was 0.10 worse, even if not statistically significantly so.

## Why this mattered

The whole experimental contract of amplify rests on **the calibrated `p` being the same `p` that gets amplified**. If amplification probes systematically operate in a different regime than calibration probes — different output budget, different success rate, different per-probe behavior — then:

1. The closed-form prediction `P_amp(N, p̂_calibration)` is plotted against an empirical curve drawn from a *different* distribution. The comparison is meaningless.
2. The "isolated arm tracks the prediction" headline of the experiment is a measurement artifact, not a property of the model or the harness.
3. The "non-isolated arm fails to track" gap, when measured, would be confounded with whatever is causing the calibration-vs-amplify discrepancy in the first place.

In short: **before we can run the amplify experiment for real, we need to understand why two ostensibly identical code paths produce statistically different per-probe behavior on the same task.**

## What we knew at the start of the investigation

- Both runs use the same `subprocess.run([..."claude", "--print", ...])` invocation via `calibrate.probe()`.
- Both runs target the same model (`claude-haiku-4-5-20251001`).
- Both runs use the same prompt template (`calibrate.PROMPT_TEMPLATE`).
- Both runs use the same CLI flags including `--max-turns 30`, `--permission-mode bypassPermissions`, `--output-format stream-json`.
- Both runs had `num_turns=1` for every probe (245/245), confirming neither used tools.
- Both runs were launched from `applications/amplify/` (so `claude --print` would auto-discover the local `applications/amplify/.claude/` directory).
- The cache_read difference (~7k tokens) suggested system-prompt context was being loaded differently somehow, but we did not know why.

## What we did NOT know

- Whether the difference was caused by the experiment design, the environment, the CLI, the API, or random noise.
- Whether the equations themselves were systematically different in difficulty between runs.
- Whether parallel-same-prompt invocation (a structural feature unique to amplify) caused the per-probe inflation.
- Whether anything in the local cwd (the auto-discovered `.claude/`) was being loaded differently across runs.
- Whether the cache state was contributing or just an artifact.

The investigation was a chain of hypothesis-and-test that progressively eliminated each of these. The next document, [01-methodology.md](01-methodology.md), lays out every test we ran and what each one was designed to answer.
