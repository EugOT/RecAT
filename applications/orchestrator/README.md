# orchestrator

A worked example of the orchestrator pattern (see [the spec README's Orchestrator section](../../README.md#orchestrator)). A *tutor* agent watches a *student* agent fail at a small task, diagnoses the failures, patches the student's instructions, and re-dispatches the student until convergence or the round budget is exhausted. The tutor is the orchestrator; the student is the worker.

The tutor's protocol is fixed across the trial. What the tutor mutates between rounds is the student's `CLAUDE.md`, held in memory and re-staged into a fresh `/tmp/` workspace for each new student invocation. After enough rounds, the student's instructions have been shaped into a program that solves the task on its own — at which point the loop terminates and the patched student stands alone.

## What this application demonstrates

Two things:

1. **Orchestrator-driven update of a worker program.** The student's instructions are rewritten between rounds by the tutor. The tutor reads the current student program, the round's failure log, and produces a new full student `CLAUDE.md` between `PATCH_BEGIN` and `PATCH_END` markers. The runner extracts the content and replaces the in-memory copy of the student program; the next round writes that updated string into a fresh `/tmp/.../.claude/CLAUDE.md` for each student invocation.

2. **A trajectory-style success-probability claim.** Unlike [`amplify`](../amplify/), which has a closed-form prediction for `P(success | I)` from the binomial CDF, orchestrator does not — there is no closed-form expression for "the probability that the orchestrator drives the student to convergence within K rounds on this initial student and this test suite." The only way to know `P(converges within K rounds | initial student, test suite)` is to run the loop many times and count.

## The task and why it forces iteration

The student is asked, on each test case, to compute the digit sum of an integer **and** a verification CHECK derived from a SHA-256 hash, then return both on a single line in this exact format:

```text
DIGIT_SUM=<S>;CHECK=<H>
```

where:

- `<S>` is the digit sum of N (sum of decimal digits) as an integer
- `<H>` is the **first 8 lowercase hex characters** of `sha256(f"{N}-{S}").hexdigest()`

For N = 12345 the correct output is `DIGIT_SUM=15;CHECK=bd6cf6e0`. The verifier (`verify/trace_verify.py`) hard-checks the line against the regex `^DIGIT_SUM=(\d+);CHECK=([0-9a-f]{8})$`.

**The CHECK component is the load-bearing iteration-forcer.** It is mathematically impossible for any language model to produce a correct 8-char SHA-256 prefix mentally — the hash function is by construction unpredictable without actual computation. The student MUST invoke `python3 -c "import hashlib; ..."` via the Bash tool to produce a passing CHECK.

The student begins with a deliberately minimal `CLAUDE.md`:

```markdown
# Student

You are a helpful assistant. Respond to user questions clearly and concisely.
```

The prompt tells the student WHAT to compute ("digit sum and verification CHECK") but says nothing about HOW (the format, the hash function, the use-Bash requirement). The tutor must teach all of those across rounds.

A typical convergence trajectory:

| Round | Likely failure mode | Tutor's patch |
| --- | --- | --- |
| 1 | Student outputs prose ("The digit sum is 15. I'm not sure what CHECK means.") | "Output exactly `DIGIT_SUM=<S>;CHECK=<H>` on one line. H is the first 8 hex chars of sha256(f'{N}-{S}')." |
| 2 | Right format, but CHECK is fabricated/wrong | "Compute CHECK with `python3 -c \"import hashlib; ...\"` via Bash. Never make up the hash." |
| 3 | All tests pass → converged | (loop exits) |

The tutor's job is to discover each fix from the verifier's precise failure reasons (`output ... does not match required format ...`, `CHECK wrong: got 'deadbeef', expected 'bd6cf6e0' (must be sha256('12345-15').hexdigest()[:8])`, etc.).

## What the verifier proves

The verifier (`verify/trace_verify.py`) is the deterministic boundary. For every trace it checks:

1. **Envelope**: well-formed JSON, monotonic step counter, run_start first, run_end last, single run_id.
2. **Structural bracketing**: each round emits `round_start → student_invoke + student_response (×N) → round_summary`, then either terminates at a verdict or emits `tutor_invoke → tutor_response` and starts the next round.
3. **Grading replay** (the load-bearing check): for every `student_response`, the verifier independently calls `grade_student_output(input, raw_output)` and confirms the runner's `passed` field is correct. A runner that mis-grades a student output gets caught here. The runner's grading and the verifier's replay use the same Python function — they cannot drift.
4. **Round-summary consistency**: `pass_count` and `pass_rate` recompute from the round's responses and must match.
5. **Termination**: the verdict is one of `converged | max_rounds | aborted`. A `converged` verdict requires the final `pass_rate` to be at or above the threshold. A `max_rounds` verdict requires `final_round == max_rounds`.

The selftest (`verify/selftest.py`) plants seven regressions and confirms the verifier rejects each with a specific reason: faked passing grade, step gap, miscounted summary, false convergence below threshold, unknown event type, bad test_index, and missing run_end.

## Workspace isolation (load-bearing)

**Both the student and the tutor run in fresh `/tmp` directories per invocation. There are no `.claude/` directories anywhere in the application source tree.** This is non-negotiable for the experiment's validity, and it has two parts.

### Part 1: no `.claude/` in the application source

Neither agent's instructions live in a `.claude/` directory inside `applications/orchestrator/`. They live as plain markdown files under `templates/`:

- `templates/tutor.md` — the tutor's instruction template
- `templates/student-initial.md` — the deliberately minimal initial student instructions

The runner reads both files into Python strings at the start of each trial. They are never loaded by Claude Code as configurations from this location, because Claude Code only auto-loads files inside `.claude/` directories. The templates exist only as input data to the runner.

### Part 2: agent invocations run from fresh `/tmp` directories

For each individual student or tutor invocation, the runner creates a fresh temporary directory via `tempfile.TemporaryDirectory(prefix="orchestrator-student-…")` and writes:

- `<tmp>/.claude/CLAUDE.md` — the agent's current instructions (from the in-memory template + any tutor patches that have been applied so far)
- `<tmp>/.claude/settings.json` — hardcoded permissions (Bash-only for the student)

Then the runner launches `claude --print` with that `<tmp>/` directory as `cwd`. The agent's `cwd` is therefore `<tmp>/`, not `applications/orchestrator/`. The agent's `.claude/` is the freshly-written one in `<tmp>/.claude/`. The temp directory is destroyed when the invocation returns.

### Why both parts matter

Without the `/tmp` cwd isolation, the student could `Read` or `Glob` the tutor's instructions, the verifier source, the test inputs, or previous traces — any of which would let it "solve" the task by reading the spec rather than learning it from the tutor's patches. We measured this directly: an earlier version of this application with the student's `cwd` inside `applications/orchestrator/` produced **2/3 pass on round 1** with the minimal "you are a helpful assistant" student; switching to `/tmp` cwd produces **0/3 pass on round 1**. The 2/3 number was a contamination artifact.

### Memory model

- The runner holds the **current student instructions in a Python variable** in `loop.py`. Initially this is the contents of `templates/student-initial.md`. Between rounds, the tutor's `PATCH_BEGIN`/`PATCH_END` block replaces the variable in memory.
- The **only** persistent on-disk record of the patched student program is the trace file (`raw_output` events from the next round contain the student's response under the patched instructions) and a **forensic dump** at `traces/<trial-id>.final-student.md` showing what the tutor produced by the end of the trial. The forensic dump is write-only — no agent ever reads it.

## Layout

```text
orchestrator/
├── README.md                      ← this file
├── templates/                     ← plain markdown templates
│   ├── tutor.md                   ← tutor instruction template, copied to /tmp/.../.claude/CLAUDE.md per invocation
│   └── student-initial.md         ← deliberately minimal initial student template, same treatment
├── inputs/
│   └── inputs.json                ← test cases, max_rounds, threshold, default trial count
├── runner/
│   ├── run.sh                     ← shell wrapper: arg parsing, cache, trial loop
│   └── loop.py                    ← per-trial loop, /tmp isolation, claude invocations, events
├── verify/
│   ├── trace_verify.py            ← envelope + structure + grading replay + termination
│   ├── trace_verify_all.sh        ← aggregator
│   └── selftest.py                ← seven planted regressions
└── traces/                        ← output: trace JSONL + conversation log + forensic .final-student.md (gitignored)
```

**Notable absence**: there is no `applications/orchestrator/.claude/` directory. Both agents run in `/tmp` workspaces that the runner creates and destroys per invocation; the only on-disk artifacts of the templates are the plain `.md` files under `templates/`, which Claude Code's auto-discovery never picks up.

## Trace artifacts and conversation logs

For one trial (`trial-001`), the `traces/` directory contains:

- `trial-001.jsonl` — the **orchestration trace** (the runner's narrative). One line per event: `run_start`, `round_start`, `student_invoke`, `student_response`, `round_summary`, `tutor_invoke`, `tutor_response`, `verdict`, `run_end`. This is the file the verifier (`verify/trace_verify.py`) reads. It is the canonical output of the trial.
- `trial-001.student-r{round}-t{test}.conversation.jsonl` — one per student invocation. **Real Claude Code stream-json output** from `claude --print --output-format stream-json --verbose`. Each line is a structured event (`system init`, `assistant`, `tool_use`, `tool_result`, `result`, ...).
- `trial-001.student-r{round}-t{test}.agent-stderr.txt` — one per student invocation. The student's stderr (typically empty unless something went wrong).
- `trial-001.tutor-r{round}.conversation.jsonl` and `.agent-stderr.txt` — same for each tutor invocation.
- `trial-001.final-student.md` — forensic dump of the final patched student instructions, written once at the end of the trial. Write-only; no agent ever reads it.

Both agents are invoked with `claude --print --output-format stream-json --verbose --max-turns 6` from a fresh `/tmp` directory. The student is constrained and auto-allowed with `--tools Bash --allowedTools Bash`; the tutor is constrained and auto-allowed with `--tools Read,Bash --allowedTools Read,Bash`; both use strict MCP config, project-only settings, disabled slash commands, and no session persistence. The runner reads the resulting stream-json from the conversation file and walks it to extract the final assistant text content for grading.

## How to run

Once a `claude` CLI is available in your PATH:

```bash
pixi run orchestrator-run -- 3        # 3 independent trials of the loop
```

This produces three JSONL trace files in `traces/`. Each trial starts from a fresh in-memory copy of `templates/student-initial.md`, so trials are independent.

Verify the results:

```bash
pixi run orchestrator-verify-all
```

The aggregator reports per-trial pass/fail and (informationally) flags any trial that converged in just 1 round, which would mean the tutor/student loop never engaged.

## Verifier self-test

Before running real trials:

```bash
pixi run selftest-orchestrator
```

Builds a faithful 3-round converged trace, plants seven regressions, confirms all are rejected with specific reasons.

## Status

The orchestrator-pattern program, the verifier (self-tested with seven planted regressions), and the runner are complete.

### Smoke run (1 trial, 3 test inputs, Haiku, real claude CLI, /tmp isolation)

**1/1 traces pass the verifier. The trial converged in 2 rounds.** The orchestrator-driven self-update loop engages end-to-end. (A successful trial uses more than one round to converge — otherwise the inter-round update claim isn't being exercised. The `trace_verify_all.sh` aggregator flags single-round convergences separately.)

What happened, round-by-round:

**Round 1** (student instructions: just `# Student\nYou are a helpful assistant. Respond to user questions clearly and concisely.`, running in an isolated `/tmp` directory with no parent visibility):

- ✗ `n=12` → "The digit sum of 12 is **3** (1 + 2 = 3). However, I need clarification on what you mean by 'verification CHECK'. Could you sp..." → multi-line prose
- ✗ `n=99` → "The **digit sum of 99** is: **9 + 9 = 18**. However, I need clarification on 'the verification CHECK' — could you explain what..." → multi-line prose
- ✗ `n=12345` → "The digit sum of 12345 is **15** (1 + 2 + 3 + 4 + 5 = 15). For the verification CHECK, the digital root (reducing to a single..." → multi-line prose with a *guessed* meaning of "CHECK"
- pass rate: **0/3 = 0.000** → loop continues

The student is genuinely confused. It correctly computes the digit sum (these are small numbers, mental arithmetic suffices) but **asks the user what "verification CHECK" means** — because nothing in its `/tmp` workspace tells it. Round 1 fails completely, exactly as the experiment intends.

**Tutor invoked.** The tutor (running in its own isolated `/tmp` directory) reads the round-1 failure log via the prompt and writes a new full student `CLAUDE.md` between `PATCH_BEGIN`/`PATCH_END` markers. The patch (preserved at [`traces/trial-001.final-student.md`](traces/)) specifies the exact `DIGIT_SUM=<S>;CHECK=<H>` format, defines `H` as the first 8 lowercase hex characters of `sha256(f"{N}-{S}").hexdigest()`, gives a worked `python3 -c "import hashlib; ..."` example for `N=12345`, and lists 4 explicit steps the student must follow.

**Round 2** (student now using the tutor's patched instructions, in a fresh `/tmp` directory):

- ✓ `n=12` → `DIGIT_SUM=3;CHECK=2c3312d0`
- ✓ `n=99` → `DIGIT_SUM=18;CHECK=672159d9`
- ✓ `n=12345` → `DIGIT_SUM=15;CHECK=bd6cf6e0`
- pass rate: **3/3 = 1.000** → **converged**

The whole trace is verifier-clean (envelope, structural bracketing, per-response grading replay, round-summary consistency, termination correctness all pass). Every `student_response.passed` field is independently re-graded by the verifier and matches.

### What this proves end-to-end

Real iteration with no contamination:

1. **The loop engages**: round 1 produces a 0/3 pass rate, confirming that without the tutor's intervention the student cannot solve the task.
2. **The tutor really reads failures and writes a real patch**: the patch is a 28-line student program that addresses every observed failure mode (prose → strict format, missing hash → use Python hashlib, extra lines → no extra lines).
3. **The patch is causally load-bearing**: round 2 passes only because the student is now running the patched instructions. No other variable changed between rounds.
4. **The verifier confirms convergence with no judgement involved**: every step, every field, every grade is independently replayed.
5. **The CHECK component cannot be faked**: the round-2 traces all contain SHA-256 prefixes that the verifier independently recomputed and matched. Haiku is genuinely calling `python3` via Bash inside the `/tmp` workspace.

### How to reproduce

```bash
pixi run orchestrator-run -- 1 --fresh --yes    # one trial, ~2 minutes on Haiku
pixi run orchestrator-verify-all                # verify the resulting trace
```

Both the student and the tutor invocations run from fresh `/tmp` directories that are cleaned up between invocations. Harness Python comes from the Pixi environment.

## See also

- [`applications/recurse/`](../recurse/) — the recursion pattern: one agent edits its own source and reruns. The contrast case for the *self-modification* shape.
- [`applications/amplify/`](../amplify/) — the composition pattern: run `N` independent copies in parallel and majority-vote, with a closed-form reliability prediction from a measured single-attempt success rate.
