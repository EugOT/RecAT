# Arithmetic — The Load-Bearing Rule

You must compute `a × b` **using only your own reasoning, written out in your response as plain text**. You may not delegate any part of the multiplication to a tool.

The verifier already knows the right answer — it computes `a × b` in Python, deterministically, before checking the trace. So the experiment isn't measuring whether the arithmetic is correct in the abstract; it's measuring **how often the model alone produces a correct answer**. A calculator collapses that measurement to 1, which makes the experiment say nothing.

## What you may NOT do

None of these are allowed for computing `a × b` or any sub-product of it:

- `python3 -c "print(a * b)"`
- `python3 -c "print(2929 * 39393)"`
- `python3 -c "print(2929 * 39000 + 2929 * 393)"`
- `expr 2929 \* 39393`
- `bc <<< "2929 * 39393"`
- `echo $((2929 * 39393))`
- `awk 'BEGIN { print 2929 * 39393 }'`
- Any MCP tool whose purpose is arithmetic (`calculator`, `wolfram`, `compute`, etc.)
- A web search for `"2929 * 39393"` or any reformulation of it
- A web search for the numeric answer
- Asking another sub-agent or model to do the multiplication
- Reading any file that might contain the answer
- Looking up a multiplication table that covers the input range

The rule covers **the entire chain of computation that leads to the answer**, not just the final operator. Splitting the multiplication into pieces and asking a tool to do each piece is the same as asking the tool to do the whole thing.

## What you MAY do

- **Long multiplication on paper.** Write the partial products in your response, in plain text, the way you would on paper.
- **Algebraic decomposition.** `2929 × 39393 = 2929 × (40000 − 607) = 2929 × 40000 − 2929 × 607`. Then compute `2929 × 40000` and `2929 × 607` *also by hand*, in your response text. The decomposition itself is allowed; what's forbidden is asking a tool to evaluate the pieces.
- **Estimation and refinement.** Compute an order-of-magnitude estimate first, then refine.
- **Cross-checks by hand.** Cast out nines (sum digits of `a`, sum digits of `b`, multiply, sum digits of your answer, check mod 9). Cast out elevens. Compute the answer two different ways and check they agree. All of this is encouraged. None of it can use a tool.
- **Use the Bash tool for trace I/O.** The `python3 -c 'import json,sys,time; ...'` idiom for emitting events to the trace file is allowed and expected. Trace I/O is not arithmetic. The distinction is in `event-emission.md`.

## Why this rule exists

The amplify experiment measures `P(model alone produces correct answer | a, b)`. The "model alone" qualifier is the entire experimental contract. If you delegate the multiplication to Python, you are no longer the system being measured — you are a thin wrapper around `python3`, and your measured `P` is `1.0` for trivial reasons that say nothing about the model.

The two-arm structure of the experiment (isolated sub-agents vs. flat-context loop, in Pass 2) only produces interesting results if the per-trial `p` is strictly between `0.5` and `1`. With a calculator, `p = 1` and there is nothing for the binomial-amplification math to amplify. The whole experiment collapses.

So the rule is not a stylistic preference. It is the experimental contract. **The parent (the harness, the verifier) is allowed and required to use Python — it is the deterministic referee. The child (you) is forbidden from using Python for arithmetic — you are the system being measured.** Honor the asymmetry. It is what makes the experiment honest.

## What the verifier can and cannot detect

The verifier (`verify/trace_verify.py`) compares your reported `answer` against `a × b` computed via Python. It does not — and cannot — inspect your scratch work to detect whether you used a calculator. If you violate this rule, your trace will probably pass verification (because calculators are right), and the experiment will be silently corrupted.

This means the rule is on the honor system, the same way most rules in `.claude/` programs ultimately are. The agent-specification project is built on the assumption that a model honestly following its instructions produces useful data, and a model gaming its instructions does not. Treat the instructions as load-bearing. Compute the multiplication by hand. The honest data is the only useful data.
