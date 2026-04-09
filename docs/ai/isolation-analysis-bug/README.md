# Isolation Analysis: Investigating the Calibration vs Amplify Discrepancy

This folder documents an investigation into an anomaly in the [amplify](../../applications/amplify/README.md) experiment. It is a complete record of the problem, the methodology used to investigate it, the data, the conclusions, and the consequences for the experimental design.

## TL;DR

**Original problem.** The amplify isolated arm (135 probes) showed a per-attempt pass rate of **0.644** vs the calibration's **0.740** on the same task class. The two runs ostensibly used identical code paths and identical model invocations. The amplify run also showed +43% output tokens and +47% duration per probe — both highly statistically significant (p<0.0001).

**Hypothesis chain we tested.**

1. *Equations are systematically harder in amplify.* — **Refuted** by a replay run that used the exact same 15 equations and got calibration-like results (0.756 pass rate).
2. *Parallel-same-prompt invocation causes per-probe inflation.* — **Refuted** by a per-probe `/tmp` isolation test. Under proper isolation, parallel and sequential are statistically indistinguishable on every feature (pass rate, output tokens, duration, cache state).
3. *Local `.claude/` auto-discovery leaks system-prompt context inconsistently.* — **Partially confirmed** as the most likely source of the original cache_read difference, fully eliminated by per-probe `/tmp` isolation.

**What we did find.** A real, arm-independent **cache outlier phenomenon**: roughly 17% of probes hit a distinct cache state where `cache_read_input_tokens` jumps from ~21k to ~44k, output tokens balloon 3–4×, and duration increases proportionally. The frequency is identical between parallel and sequential arms. We have characterized but cannot explain this phenomenon — it lives on Anthropic's serving infrastructure.

**What this means for the experiment.** The right pattern is per-probe `/tmp` isolation modeled on `applications/orchestrator/runner/loop.py:invoke_claude_isolated()`. Each probe runs in its own `tempfile.TemporaryDirectory()` with a freshly written minimal `.claude/`. This eliminates cwd-dependent behavior, eliminates leakage from auto-discovered local config, and produces clean reproducible results.

The cache outlier remains a real noise source that future amplify runs need to either filter post-hoc or absorb with larger M.

## Document index

| File | Contents |
| --- | --- |
| [README.md](README.md) | This index |
| [00-problem.md](00-problem.md) | The original anomaly: what we observed, why it mattered |
| [01-methodology.md](01-methodology.md) | Every test we ran, in chronological order, with the question each one was designed to answer |
| [02-findings.md](02-findings.md) | Statistical comparisons, feature distributions, p-values |
| [03-conclusions.md](03-conclusions.md) | What we ruled out, what we found, the deductive chain |
| [04-impact.md](04-impact.md) | How this affects the amplify experiment design and what to do next |
| [05-references.md](05-references.md) | Documentation citations (Anthropic docs, Claude Code docs) and code references |
| [data/](data/) | Raw report.json / results.json files from every run discussed |

## How to read this

Read in order: 00 → 01 → 02 → 03 → 04. The references and raw data are supporting material.

If you only have time for one document, read [03-conclusions.md](03-conclusions.md). It is the deductive summary that ties everything together.

## Reproducibility

All raw data is in [data/](data/). Every test mentioned in this analysis can be reproduced by running the relevant script in `applications/amplify/runner/`. The throwaway investigation scripts (`calibrate_replay.py`, `parallel_vs_sequential.py`, `probe_isolated_test.py`) were deleted after the investigation was complete; their JSON outputs are preserved in `data/`.

## Authorship

This investigation was performed in a single Claude Code session on 2026-04-08, in collaboration with the user. The narrative chronology of the investigation is documented in [01-methodology.md](01-methodology.md).
