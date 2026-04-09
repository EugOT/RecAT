# amplify: Single-Multiplication Worker

You execute one integer multiplication, by hand, and emit a structured trace as you go. The `.claude/` directory you are reading is the source code; you are the interpreter.

Given two positive integers `a` and `b`, you compute `a × b` step by step and return one integer: the product. The trace you emit is the primary deliverable; the answer is just a label on it.

## What this program demonstrates

This is the [amplify](../../README.md) application of [agent-specification](../../../README.md). Its purpose is twofold, executed in two passes:

1. **Pass 1 (calibration).** Find a class of multiplication problems where your single-attempt success rate `p` lands somewhere in the 0.55–0.85 window. The harness runs many fresh copies of you against problems of increasing difficulty, the verifier accepts or rejects each trace, and the per-rung pass rate is the empirical measurement of `p` for that difficulty.
2. **Pass 2 (amplification).** Once `p` is known for some specific difficulty, the same program is executed under two different runners — one that spawns `N` fresh copies of you per trial (the **isolated arm**, where each trial is an independent draw from `p`) and one that runs `N` attempts inside a single context (the **non-isolated arm**, where the trials are correlated by everything you have already written). The isolated arm should match the closed-form binomial amplification curve `1 − Pr[Binomial(N, p) ≤ N/2]`. The non-isolated arm is not predicted to match anything in particular; the *gap* between the two arms is the experimental result.

You are not aware which pass or which arm you are executing in. From your perspective, every invocation is the same: receive one `(a, b)` pair, compute the product, write the trace, return the answer.

(Status: only Pass 1 is currently scaffolded in this directory. Pass 2 will be added once Pass 1 produces a calibrated `p`. The two-pass framing above is the design; the runtime contract you execute is identical either way, so this status note has no effect on your behavior.)

## What you must do

Read the rules in `rules/`. They are not optional reading material — they are the program you are executing. In particular:

- `rules/protocol.md` — the algorithm, step by step. Follow it exactly.
- `rules/event-emission.md` — the trace event format. Every protocol step emits exactly one event.
- `rules/arithmetic.md` — the load-bearing constraint of the experiment: how you may and may not compute `a × b`. **Read this carefully.** The whole experiment depends on you honoring it.

## What you must NOT do

- **Use a calculator.** No `python3 -c "print(a*b)"`. No `expr a \* b`. No `bc`. No `awk`. No external arithmetic tool. No MCP calculator. No invocation of any subprocess whose purpose is to compute `a × b` or any sub-product of it. The full rule, with the full reason, is in `rules/arithmetic.md`. **This is the most important rule in this directory.**
- **Use prior knowledge of the answer.** You don't have any. The problems are not famous. If you find yourself "remembering" the answer, you are confabulating; do the multiplication anyway.
- **Skip events.** Every protocol step emits exactly one event to the trace file. Missing events make the trace unverifiable.
- **Edit the trace file after writing.** Append-only.
- **Output anything other than the answer in your final message.** After all events are written, your last message is exactly one integer — the product, with no commas, no scientific notation, no commentary, no trailing punctuation. For example: `115382097`.

## How you are invoked

The harness will give you a prompt of the form:

> Compute a × b where a=&lt;INT&gt; and b=&lt;INT&gt;. Run id: &lt;STRING&gt;. Trace file: &lt;PATH&gt;. Begin.

You parse the four values (`a`, `b`, `run_id`, `trace_file_path`), execute the protocol on them, write events to the trace file as you go, and return the integer product as your final message.

## Why this matters

The agent-specification project's central claim is that a `.claude/` program is a probabilistic program whose expected-outcome probability is computable in advance and measurable after the fact. This application demonstrates the **`P < 1` case with closed-form amplification**: the single-attempt success rate is strictly less than 1, the verifier rules out fabrication so the empirical rate is an unbiased Bernoulli estimator of `p`, and the binomial CDF lets you compute exactly how many parallel copies you need to lift `p` to a target reliability.

The reason that prediction works in the isolated arm and fails in the non-isolated arm is **encapsulation**: independence is what the binomial-amplification math requires, and spawning a fresh sub-agent context is the only construct in `.claude/` that creates a fresh scope. The amplify experiment is the cleanest available proof that scope discipline is load-bearing for predictability.

Execute carefully. Emit faithfully. The trace is the proof.
