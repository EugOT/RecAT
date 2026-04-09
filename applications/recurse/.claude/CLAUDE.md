# recurse: parent agent

You are the **parent agent** of `recurse`. This file
(`.claude/CLAUDE.md` in the source tree) is your protocol. **You do
not edit this file during a run.** What you DO edit, across
iterations, is `state/agent-program.md` (a sibling file in the same
directory), which holds the *current version of a recursive child
program* you are improving.

The recursion: each iteration you copy `state/agent-program.md` into
a fresh `/tmp/recurse-iter-NNN/` workspace as the child's
auto-loaded instructions, spawn a child `claude` invocation there,
read what the child produced, grade it against ground truth using
`verify/grade.py` (which lives in the source tree and is **never**
copied to `/tmp/`), and then `Edit` the single file
`state/agent-program.md` to be a better version for the next
iteration. The child is a copy of the current `agent-program.md`;
you mutate `agent-program.md`; the next child is a copy of the
mutated version. That is the recursive self-modification loop.

## What you are doing (the task)

A hidden integer-to-integer function `f(x)` exists. Its parameters
are in `inputs/inputs.json` under the chosen instance. You can `Read`
this file directly because you are the parent. The child copy in
`/tmp/` cannot — `inputs/` is never copied to `/tmp/`.

Ground-truth formula: `f(x) = ((a*x + b) ^ c) % m`.

**Critical: in Python, `^` is the bitwise XOR operator, NOT
exponentiation.** `5 ^ 0` is `5`, not `1`. `0 ^ 0` is `0`. `0xA5 ^
0x3C` is `0x99`. **Do not compute `f(x)` in your head — always shell
out** to `python3 -c 'print(((a*x+b)^c)%m)'` with the actual
parameter values you read from `inputs/inputs.json`. Computing
ground truth wrong is the single fastest way to ruin a trial.

Your job is to drive `state/agent-program.md` through edits until a
copy of it, when run as a child claude in `/tmp/`, produces an
`ANSWER` block containing a Python lambda that computes `f(x)` for
*every* integer `x` in `[0, x_domain_max]`.

## Tools you will use

- `Read` to load `state/agent-program.md`, `inputs/inputs.json`, and
  child output files from `/tmp/`.
- `Bash` to: shell out to `python3` for ground-truth computation;
  invoke `bin/setup-iter.sh` to set up child workspaces; invoke
  `claude --print` to spawn children; invoke `verify/grade.py` to
  grade.
- `Write` to append iteration records to
  `traces/iterations/iter-NNN.md` and the final verdict to
  `traces/verdict.txt`.
- `Edit` (or full-file `Write`) to update `state/agent-program.md`
  between iterations.

**Never use any Bash command containing the literal substring
`.claude` in its arguments**, even pointing at /tmp paths. Claude
Code's path policy will block it. The `bin/setup-iter.sh` script
exists *specifically* to do the `.claude/` setup from outside your
tool-call context — call it via Bash and let it do the work.

## The loop

### Step 0: setup

1. `Read state/agent-program.md` — this is the current child program.
2. `Read inputs/inputs.json` — find the instance whose `id` matches
   the `instance_id` in your starting prompt. Note its
   `a, b, c, m` and the globals `x_domain_max` and `max_iterations`.
   **Hold these in your context. Never write them to any file.**
3. Pick 6 well-spread test inputs from `[0, x_domain_max]`. Use
   `[0, 1, 7, 23, 100, 175]` unless you have a reason to deviate.
4. Compute the 6 expected outputs by Bash:
   `python3 -c 'a,b,c,m=A,B,C,M; print([((a*x+b)^c)%m for x in [0,1,7,23,100,175]])'`
   substituting the actual numbers. Memorize the result.
5. Write `traces/iterations/setup.md` with: instance id, the 6 test
   inputs, the 6 expected outputs, and the initial sha256 of
   `state/agent-program.md` (use `shasum -a 256 state/agent-program.md`).

### Step N: one iteration (start with N=1)

In every reference below, the literal `N` should be substituted with
the **zero-padded** iteration number: `iter-001`, `iter-002`, …,
`iter-006`. Always 3 digits. The /tmp directory follows the same
convention: `/tmp/recurse-iter-001/`,
`/tmp/recurse-iter-002/`, etc.

1. **Set up the child workspace** by invoking the helper script:
   `Bash: ./bin/setup-iter.sh /tmp/recurse-iter-NNN state/agent-program.md`
   (substitute `NNN` with the zero-padded iteration number). The
   script creates `/tmp/recurse-iter-NNN/` with the child's
   auto-loaded instructions and Bash-only settings inside it. Output
   ends with `OK <path>` on success.
2. **Compose the child's task prompt.** The prompt must contain:
   (a) two example pairs the child can use for inference (use `x=0`
   and `x=1`; compute `f(0)` and `f(1)` via Bash if you don't already
   have them: `python3 -c 'print(((4*0+11)^0)%256, ((4*1+11)^0)%256)'`
   substituting the real parameters);
   (b) the 6 test inputs;
   (c) the exact instruction "Compute f(x) for the listed inputs.
   Emit your final answer as a single Python lambda between
   ANSWER_BEGIN and ANSWER_END markers, then stop. Do not request
   more examples."
3. **Spawn the child** via Bash from the iteration directory:
   `cd /tmp/recurse-iter-NNN && claude --print "<the prompt>"
    --model claude-haiku-4-5-20251001 --max-turns 6
    --permission-mode bypassPermissions > child-output.txt
    2> child-stderr.txt`
4. **Read the child's output**: `Read /tmp/recurse-iter-NNN/child-output.txt`.
5. **Extract the lambda** from the child's output by looking for the
   first `ANSWER_BEGIN`/`ANSWER_END` block. If there is no such
   block, treat the iteration as a child malformed-response and
   skip to step 7 with `verdict=malformed`.
6. **Grade the lambda** by Bash:
   `python3 verify/grade.py "<the lambda exactly as one line>" <a> <b> <c> <m> <x_domain_max>`
   The grader prints `RESULT: PASS` or `RESULT: FAIL <reason>`.
   **Do not skip the grader.** Do not grade in your head.
7. **Append to `traces/iterations/iter-NNN.md`**: the child's full
   raw output, the extracted lambda, the grader's verdict line, and
   the sha256 of `state/agent-program.md` BEFORE this iteration.
8. **Decide whether to continue:**
   - Grader says PASS → append `VERDICT: solved at iteration N` and
     stop the loop.
   - `N >= max_iterations` → append `VERDICT: fuel_exhausted` and stop.
   - Otherwise continue to step 9.
9. **Edit `state/agent-program.md`** based on what went wrong. The
   child program is short markdown — read it, identify why the child
   failed, and either `Edit` it or rewrite it with `Write`. Goals
   for your edit:
   - Make the child's task description sharper.
   - Add hints about the function shape (linear-mod, possibly XOR
     mask) WITHOUT writing the actual `a, b, c` values.
   - Tell the child to compute the lambda from the example pairs
     using `python3` via Bash (the child has Bash) instead of
     guessing.
10. **Hash check after edit**: Bash
    `shasum -a 256 state/agent-program.md` and append the new hash
    to `traces/iterations/iter-NNN.md`. If the new hash equals the
    old hash, append `WARNING: no mutation this iteration` — your
    edit was a no-op and the recursion is not engaging.
11. Increment N. Go to step 1.

### Termination

When the loop ends, `Write` a single-line verdict to
`traces/verdict.txt`:

```text
result=<solved|fuel_exhausted|malformed> instance=<id> iterations=<N> final_hash=<sha256>
```

Stop. Do not delete the `/tmp/recurse-iter-*` directories;
leave them for the user to inspect.

## Hard rules

- **`^` is bitwise XOR in Python, not exponentiation.** Always
  compute ground truth via `python3 -c`.
- **Never copy `verify/`, `inputs/`, or `.claude/` into `/tmp/`.**
  The `bin/setup-iter.sh` script only copies `state/agent-program.md`.
- **Never write the literal parameter values `a, b, c` into
  `state/agent-program.md` or any file in `/tmp/`.**
- **Never edit this file (`.claude/CLAUDE.md`).**
- **Never put `.claude` in a Bash command's arguments.** Use
  `bin/setup-iter.sh` for the workspace setup; let it do the
  hidden-directory writes from outside your tool-call context.
- **The child gets Bash only** (`bin/setup-iter.sh` configures this).
- **Use `max_iterations` from `inputs/inputs.json` as the hard cap.**

## Why the design has this shape

This is "close enough to recursion": strict recursion would mean
*you* (the parent) edit your own `.claude/CLAUDE.md`, but that file
is tracked in the source tree and we don't want to dirty it.
Instead, the **mutable self** is `state/agent-program.md` — a
sibling file the parent edits across iterations and copies into
`/tmp/` per child invocation. Each iteration is
`child(N) = copy_of(self(N))` followed by
`self(N+1) = edit(self(N), observation_of(child(N)))`. The
recursion is the loop over `N`. The parent's `.claude/` is the fixed
protocol that drives the loop; the child's `.claude/` is the
recursively-mutated copy of the mutable self.
