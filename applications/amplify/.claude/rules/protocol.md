# Multiplication Protocol

This is the algorithm you execute. Follow it step by step. After every step, emit exactly one trace event using the format in `event-emission.md`.

Throughout this document:

- `a` and `b` are the two positive integers being multiplied. They come from the prompt.
- `run_id` and the trace file path are provided in the prompt.
- The step counter starts at 0 with `run_start` and increments by 1 for every subsequent event.

## Step 0 — `run_start`

Emit:

```json
{"run_id": "<run_id>", "step": 0, "ts": <ts>, "event": "run_start", "data": {"a": <a>, "b": <b>, "encoding": "claude-agent"}}
```

This is always the first event. After this, every subsequent event increments the step counter.

## Step 1 — Compute `a × b` by hand

This is the only computation step in the algorithm and it is the entire point of the experiment. Read `arithmetic.md` for what you may and may not do. In summary: do the multiplication in your own scratch reasoning — written out as part of your normal response text — using long multiplication, decomposition, distributive expansion, or any other paper-and-pencil technique you would use without a calculator. **Do not call out to any subprocess that performs the multiplication or any of its sub-products.**

You may take as much scratch space as you need. You may show work. You may sanity-check the result with a mod-9 or mod-11 cast-out check. You may try the multiplication two different ways and reconcile. None of this counts as cheating. The only thing that counts as cheating is delegating the arithmetic to a tool.

When you have an answer you are confident in, **call it `answer`** and proceed to Step 2.

## Step 2 — `final_answer`

Emit:

```json
{"event": "final_answer", "data": {"a": <a>, "b": <b>, "answer": <answer>}}
```

The `answer` field must be the integer product, encoded as a JSON number (not a string). The `a` and `b` fields must match the values from `run_start` exactly — they are repeated here so the verifier can confirm the trace is internally consistent.

## Step 3 — `run_end`

Emit:

```json
{"event": "run_end", "data": {"answer": <answer>}}
```

The `answer` field must equal the value emitted in `final_answer`. Mismatches are a hard failure.

## Step 4 — Final message

After emitting `run_end`, your final response is exactly one integer: the product, with no commas, no spaces, no scientific notation, no commentary, no trailing punctuation. Example final messages:

- `3901`
- `132678`
- `115382097`

This is what the harness reads to confirm the run terminated normally. The trace is what the verifier reads to confirm the run was honest.
