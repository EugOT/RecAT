# Event Emission

You write structured events to the trace file as you execute the protocol. The trace file path is provided in the prompt. Every protocol step emits exactly one event. Skipping events makes the trace unverifiable; the trace verifier rejects incomplete traces.

## Schema

Each event is one line of JSON appended to the trace file:

```json
{"run_id": "<from prompt>", "step": <int>, "ts": <float>, "event": "<type>", "data": {<payload>}}
```

Field meanings:

- **`run_id`** — the run identifier from the prompt. Identical for every event in the run.
- **`step`** — a non-negative integer. Starts at 0 for `run_start` and increments by exactly 1 for each subsequent event. You track the counter yourself.
- **`ts`** — floating-point seconds since the Unix epoch. Set automatically by the Python emit idiom below. You do not set this field yourself.
- **`event`** — one of the legal event types listed below. Inventing new event types is forbidden.
- **`data`** — an event-specific JSON object. The required fields depend on the event type and are documented in `protocol.md`.

## How to write an event

Every event is emitted through `python3` with this exact canonical idiom:

```bash
python3 -c 'import json,sys,time; d=json.loads(sys.argv[1]); d["ts"]=time.time(); print(json.dumps(d))' \
  '{"run_id":"<run_id>","step":<step>,"event":"<type>","data":{...}}' \
  >> <trace_file>
```

You pass the event body (without the `ts` field) as a single shell argument in single quotes. Python parses the JSON, adds a real Unix timestamp, re-serializes, and appends one line to the trace file.

**This is the only acceptable way to emit events.** Do not use `echo`. Do not use `cat <<EOF`. Do not use `printf`. Do not hand-assemble the JSON with shell variable expansion. The Python idiom validates the JSON before writing and stamps a real timestamp on every call.

### Important: this Python call is for trace I/O, not arithmetic

You will use `python3` via the Bash tool to emit events. That is allowed. What is **not** allowed is using `python3` (or any other tool) to compute `a × b` or any sub-product of it. The distinction is sharp:

- **Allowed:** `python3 -c 'import json,sys,time; ...'` to format and append a trace event.
- **Forbidden:** `python3 -c "print(2929 * 39393)"` to compute the answer.
- **Forbidden:** `python3 -c "print(2929 * 39000)"` to compute a sub-product.
- **Forbidden:** Any invocation whose argument string contains the operator `*` between two literal numbers from the input.

The verifier and the trace alone cannot perfectly distinguish these cases (a sufficiently determined agent could obfuscate), but the rule is clear and you are expected to honor it. See `arithmetic.md` for the full reasoning.

### Rules for the argument

- **Use single quotes** around the JSON body argument.
- **Do not include a literal single quote** inside the event body.
- **Do not include a `ts` field** in the argument; the Python snippet adds it.
- **Substitute your actual values** for `<run_id>`, `<step>`, `<type>`, and the data fields.

### Worked example

```bash
python3 -c 'import json,sys,time; d=json.loads(sys.argv[1]); d["ts"]=time.time(); print(json.dumps(d))' \
  '{"run_id":"rung4-p0-t001","step":0,"event":"run_start","data":{"a":2929,"b":39393,"encoding":"claude-agent"}}' \
  >> /tmp/.../rung4-p0-t001.jsonl
```

## Legal event types

These are the only legal event types. The trace verifier rejects unknown event types.

- `run_start` — first event of every run. Required data: `a`, `b`, `encoding`.
- `final_answer` — the product, computed by hand. Required data: `a`, `b`, `answer`.
- `run_end` — last event of every run. Required data: `answer`.

That is the entire schema. The amplify program is intentionally minimal — there are exactly three events per run, and no protocol branching. The complexity lives in the harness and the verifier, not in the program.

## Critical rules

1. **Every protocol step emits exactly one event.** No bundling. No skipping.
2. **Step counts are strictly increasing.** Start at 0, increment by 1, never repeat or skip a number.
3. **Events are in execution order.** `run_start` first, then `final_answer`, then `run_end`.
4. **Each event is one JSON object on one line.** No multi-line JSON. No comments. No trailing commas.
5. **Append-only.** Never edit the trace file after writing.
6. **One trace file per run.**

## What gets verified

The trace verifier (`verify/trace_verify.py`) checks:

- Every event is valid JSON conforming to the schema.
- Step counts start at 0, are strictly increasing, and have no gaps.
- The first event is `run_start` and the last event is `run_end`.
- The middle event is `final_answer`.
- Every event type is one of the three legal types.
- The `a` and `b` in `final_answer.data` match the `a` and `b` in `run_start.data`.
- The `answer` field in `final_answer.data` is an integer.
- The `answer` field in `run_end.data` matches the one in `final_answer.data`.
- **The headline check:** `final_answer.data.answer == a × b`, where `a × b` is computed independently by Python in the verifier from the values in `run_start.data`. This is the Bernoulli draw — PASS means the agent got the answer right, FAIL means it got the answer wrong.

The trace is the proof of the run; the headline check is the proof of correctness.
