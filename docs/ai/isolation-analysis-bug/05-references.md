# 05 — References

## Anthropic and Claude Code documentation

These were located by an automated documentation search agent and used to interpret the empirical findings. Direct quotes are included where they bear on the conclusions in this analysis.

### Prompt caching — **most relevant**

**URL.** https://platform.claude.com/docs/en/build-with-claude/prompt-caching.md

**Direct quote:**
> "For concurrent requests, note that a cache entry only becomes available after the first response begins. If you need cache hits for parallel requests, wait for the first response before sending subsequent requests."

**Relevance.** This documents a real race condition in Anthropic's prompt caching layer for concurrent requests. When N identical requests fire simultaneously, the first one starts populating the cache, but the others fire *before* the first response begins, so they cannot see the cache yet and may all create their own cache entries. This is a documented mechanism that COULD explain cache_creation/cache_read accounting differences between concurrent and sequential identical requests.

**However**, this only directly explains *cache accounting* differences. The docs do NOT explain why concurrent identical requests would produce *longer outputs* or *lower accuracy*. And our [Test 7](01-methodology.md#test-7--parallel-vs-sequential-per-probe-tmp-isolation) showed that under per-probe `/tmp` isolation, parallel and sequential are statistically identical on every feature, including cache_read. So while the documented race condition is real, it does not appear to be the cause of the observed inflation in our specific setup.

### Rate limits

**URL.** https://platform.claude.com/docs/en/api/rate-limits

**Key facts:**
- API uses a token bucket algorithm: "your capacity is continuously replenished up to your maximum limit, rather than being reset at fixed intervals."
- "Short bursts of requests can exceed the limit and trigger rate limit errors."
- For Claude Haiku 4.5 at Tier 3: 2,000 RPM, 1,000,000 ITPM.
- No documented differential backend routing for burst vs sequential traffic.
- No documented "slower backend" or "fast backend" routing based on concurrency pattern.

**Relevance.** Burst throttling exists but does not appear to explain the observed inflation. The amplify run was nowhere near the documented Tier 3 RPM/ITPM caps. And under per-probe `/tmp` isolation we showed that parallel and sequential have identical behavior — if rate limiting were the cause, we would expect parallel to be slower or different.

### Claude Code CLI reference

**URL.** https://code.claude.com/docs/en/cli-reference

**Key facts:**
- `--print` flag: "Print response without interactive mode"
- "Fixed issue where `claude -p` would hang when spawned without explicit stdin (e.g., via Python `subprocess.run`)" — this is the only documented concurrency-related fix for `--print`.
- No other documented warnings or behaviors specific to running parallel `--print` subprocesses.

**Relevance.** No documented warnings about parallel `--print` invocations. The earlier hang bug (which we have not encountered) confirms that some attention has been paid to subprocess behavior, but no broader concurrency caveats are published. We use `stdin=subprocess.DEVNULL` to avoid the historical hang.

### Claude Agent SDK overview

**URL.** https://platform.claude.com/docs/en/agent-sdk/overview.md

**Direct quote on subagent concurrency:**
> "Spawn specialized agents to handle focused subtasks... You can run up to 10 of them simultaneously."

**Relevance.** The Agent SDK actively encourages parallel subagent execution (up to 10), with no caveats about identical prompts. This is consistent with our finding that parallel-on-different-prompts works fine; it does not directly bear on our specific anomaly.

### Settings.json permissions schema

**URL.** Inferred from `applications/orchestrator/runner/loop.py:STUDENT_SETTINGS_JSON` and `TUTOR_SETTINGS_JSON`.

**Pattern:**
```json
{"permissions": {"allow": ["Bash", "Read"]}}
```

In our isolated probe primitive we use:
```json
{"permissions": {"allow": []}, "autoMemoryEnabled": false, "hooks": {}}
```

The empty allow list denies all tools. This is the load-bearing piece that prevents the model from reaching for `python3` via `Bash` to compute the multiplication, which would silently break the experimental contract by giving the model a calculator.

## Code references

### Orchestrator's per-probe `/tmp` isolation pattern

**File.** `applications/orchestrator/runner/loop.py`

**Function.** `invoke_claude_isolated()` (line 135)

**Key invariant from the file's docstring:**
> "Isolation invariant: no subprocess in this trial ever has its cwd inside `applications/orchestrator/`, and no `.claude/` directory exists inside the application source tree. Both the student and the tutor run in `/tmp`."

This is the pattern we adapted for our isolated probe primitive. Each invocation creates a fresh `tempfile.TemporaryDirectory()` with a per-invocation `.claude/CLAUDE.md` and `settings.json`, runs `claude --print` with `cwd=` set to that tempdir, and the directory is auto-deleted on context exit.

**Why it works.** `claude --print` walks UP from cwd to discover `.claude/` directories. By starting in a fresh `/tmp/...` with no parent `.claude/` chain back to the experiment source, the only context the child can possibly load is the minimal `.claude/` we wrote into the tempdir, plus `~/.claude/` (always loaded), plus Anthropic's base system prompt.

### Calibrate / Amplify probe code

**File.** `applications/amplify/runner/calibrate.py`

**Function.** `probe()` (line 236)

**Current implementation.** Spawns `claude --print` from the parent process's cwd (whatever that happens to be — usually `applications/amplify/`). This means the child auto-discovers `applications/amplify/.claude/CLAUDE.md` and the `rules/` directory.

**Recommended change.** Wrap the subprocess in `tempfile.TemporaryDirectory()` and write a minimal `.claude/CLAUDE.md` + `settings.json` per invocation. Modeled on `orchestrator/runner/loop.py:invoke_claude_isolated()`. See [04-impact.md](04-impact.md#2-the-amplify-code-path-needs-to-switch-to-per-probe-tmp-isolation).

### The deleted scripts

The investigation produced four throwaway scripts that were deleted after their data was captured. The scripts no longer exist in `runner/`, but their JSON outputs are preserved in [data/](data/):

1. `runner/calibrate_replay.py` → [data/03-replay-results.json](data/03-replay-results.json)
2. `runner/parallel_vs_sequential.py` → [data/04-parvseq-shared-cwd-results.json](data/04-parvseq-shared-cwd-results.json)
3. `runner/probe_isolated_test.py` → [data/05-isoprobe-tmp-isolation-results.json](data/05-isoprobe-tmp-isolation-results.json)

The first one (`calibrate_replay.py`) ran 45 probes on the same 15 equations as the original amplify run, in parallel batches on different equations. The second one ran 9 parallel + 9 sequential identical probes from a shared cwd. The third one repeated the second with per-probe `/tmp` isolation.

If a future investigator wants to re-run any of these, the methodology is fully documented in [01-methodology.md](01-methodology.md) and the implementation pattern is straightforward.

## Statistical methods used

- **Two-proportion z-test** for comparing pass rates between groups.
- **Mann-Whitney U test (normal approximation)** for comparing distributions of continuous features (output_tokens, duration_ms, cache_read, etc.) without assuming normality. The within-arm distributions of these features are heavily right-skewed, so a t-test would be inappropriate.
- **Wilson 95% confidence interval** for binomial proportions, preferred over the normal-approximation Wald interval for small or extreme proportions.

All statistical computations are inline in the throwaway scripts and the analysis snippets in [01-methodology.md](01-methodology.md). No external statistics libraries are used (everything is plain Python `math` + `statistics`), so the calculations are easy to audit and reproduce.

## Glossary

- **`cache_read_input_tokens`** — number of input tokens served from Anthropic's prompt cache for this request. Reported in the `usage` field of the `result` event in stream-json output.
- **`cache_creation_input_tokens`** — number of input tokens written into the prompt cache by this request (i.e., new content not previously cached).
- **`output_tokens`** — number of assistant-text tokens generated by the model in the response.
- **`num_turns`** — number of assistant turns in the response. `1` means the model wrote one response and stopped. `2+` means the model used a tool, got a result, and wrote another response (i.e., it called Bash, Read, etc.).
- **probe** — one independent invocation of `claude --print` for one experiment trial.
- **arm** — one of the experimental conditions being compared (e.g., parallel vs sequential, isolated vs non-isolated).
- **cache outlier / cache anomaly** — a probe that received `cache_read_input_tokens ≈ 43,735` instead of the normal ≈ 20,965. ~17% of probes in our test, mechanism unknown.
- **per-probe `/tmp` isolation** — the experimental pattern where each probe runs in its own freshly-created `tempfile.TemporaryDirectory()`, with no shared filesystem state with any other probe.
