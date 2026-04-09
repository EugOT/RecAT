#!/usr/bin/env bash
#
# amplify runner — Pass 1 (calibration).
#
# Invokes the .claude/ amplify program on every (rung, problem) pair in
# inputs/inputs.json, repeated TRIALS times per pair. Each invocation
# produces one JSONL trace file in traces/. The verifier accepts or rejects
# each trace; the aggregator groups results by rung and reports per-rung
# pass rates, which IS the calibration measurement of `p` for that rung.
#
# The agent (the language model) IS the program. This script is just the
# harness that invokes it. Its job is to:
#
#   1. Read the rungs and problems from inputs/inputs.json.
#   2. For each (rung, problem, trial), invoke `claude` headlessly with a
#      prompt that tells the agent the inputs (a, b), the run id, and the
#      trace file path.
#   3. Let the agent execute, write events to the trace file, and exit.
#   4. Move on to the next pair.
#
# The agent's output is the trace JSONL. This script does not analyze it.
# The trace verifier (verify/trace_verify.py) consumes traces/ as a
# separate step and is the deterministic proof of correctness.
#
# Usage:
#   ./runner/run.sh                # use trials_per_problem_default from inputs.json
#   ./runner/run.sh 5              # 5 trials per problem
#   ./runner/run.sh 10             # 10 trials per problem (recommended for calibration)
#
# Requirements:
#   - python3 in PATH (used to parse inputs.json and to emit trace events
#     from the agent — note: the agent is FORBIDDEN from using python3 to
#     compute the multiplication itself; see .claude/rules/arithmetic.md)
#   - claude CLI in PATH, capable of headless invocation
#   - The cwd when invoking `claude` is the amplify directory, so that
#     the .claude/ in this directory is loaded.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INPUTS_FILE="$ROOT/inputs/inputs.json"
TRACES_DIR="$ROOT/traces"
mkdir -p "$TRACES_DIR"

# Flag parsing. The legacy positional argument is TRIALS (an integer), so
# we accept a bare integer as shorthand for --trials N. Named flags can
# appear in any order.
TRIALS_ARG=""
CACHE_MODE=""  # "" | "resume" | "fresh"
YES=""
RUNG_FILTER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trials)
            TRIALS_ARG="$2"
            shift 2
            ;;
        --rung)
            RUNG_FILTER="$2"
            shift 2
            ;;
        --resume)
            CACHE_MODE="resume"
            shift
            ;;
        --fresh)
            CACHE_MODE="fresh"
            shift
            ;;
        --yes|-y)
            YES="1"
            shift
            ;;
        -h|--help)
            cat >&2 <<'EOF'
./runner/run.sh [TRIALS] [--rung N] [--resume | --fresh] [--yes]

Run the amplify Pass 1 calibration. Writes one JSONL trace per
(rung, problem, trial) to traces/.

Positional:
  TRIALS         trials per problem (default: trials_per_problem_default
                 from inputs.json). Equivalent to --trials N.

Filtering:
  --rung N       run only the rung with the given numeric index. Useful
                 for climbing the staircase one rung at a time.

Cache control (required on re-runs):
  --resume       Skip any run that already has a complete trace (one with
                 a run_end event) and execute only the remaining work.
  --fresh        Delete every file in traces/ and re-run the whole batch
                 from scratch. Requires --yes when invoked non-interactively.
  --yes, -y      Skip destructive-action confirmation prompts.

If traces/ is empty, no cache flag is required. If any complete traces
already exist, one of --resume or --fresh is mandatory.
EOF
            exit 0
            ;;
        [0-9]*)
            TRIALS_ARG="$1"
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# Default trials per problem: read from inputs.json unless overridden on the command line.
# Pass the path as argv[1] rather than interpolating into source, so paths
# containing quotes or other shell metacharacters can't break the script.
DEFAULT_TRIALS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["trials_per_problem_default"])' "$INPUTS_FILE")
TRIALS="${TRIALS_ARG:-$DEFAULT_TRIALS}"

# Read the test matrix and emit (rung_index, rung_label, problem_index, a, b)
# tuples, one per line, tab-separated so labels containing spaces are safe.
# Optionally filter by rung.
PAIRS=$(python3 -c '
import json, sys
inputs = json.load(open(sys.argv[1]))
rung_filter = sys.argv[2]
for rung in inputs["rungs"]:
    if rung_filter and str(rung["rung"]) != rung_filter:
        continue
    for pi, prob in enumerate(rung["problems"]):
        print("\t".join([str(rung["rung"]), rung["label"], str(pi), str(prob["a"]), str(prob["b"])]))
' "$INPUTS_FILE" "$RUNG_FILTER")

if [ -z "$PAIRS" ]; then
    if [ -n "$RUNG_FILTER" ]; then
        echo "ERROR: no problems found for rung $RUNG_FILTER" >&2
    else
        echo "ERROR: no problems found in $INPUTS_FILE" >&2
    fi
    exit 1
fi

TOTAL_PAIRS=$(echo "$PAIRS" | wc -l | tr -d ' ')
TOTAL_RUNS=$((TOTAL_PAIRS * TRIALS))

# Regex for detecting complete traces.
RUN_END_PATTERN='"event"[[:space:]]*:[[:space:]]*"run_end"'

# Cache-mode gate. Detect whether any complete traces already exist; if
# so, require the user to pick --resume or --fresh.
has_complete=0
if [ -d "$TRACES_DIR" ]; then
    shopt -s nullglob
    for f in "$TRACES_DIR"/*.jsonl; do
        case "$f" in *.conversation.jsonl) continue ;; esac
        if [ -s "$f" ] && grep -qE "$RUN_END_PATTERN" "$f"; then
            has_complete=1
            break
        fi
    done
    shopt -u nullglob
fi

if [ "$has_complete" -eq 1 ] && [ -z "$CACHE_MODE" ]; then
    echo "ERROR: $TRACES_DIR already contains complete traces." >&2
    echo "" >&2
    echo "Choose one of:" >&2
    echo "  --resume    Skip any run that already has a complete trace and" >&2
    echo "              execute only the remaining work." >&2
    echo "  --fresh     Delete every file in traces/ and re-run the whole" >&2
    echo "              batch from scratch. Add --yes to skip the prompt" >&2
    echo "              when invoking non-interactively." >&2
    echo "" >&2
    exit 1
fi

if [ "$CACHE_MODE" = "fresh" ]; then
    echo "⚠  --fresh: will delete ALL files in $TRACES_DIR/" >&2
    if [ -z "$YES" ]; then
        if [ -t 0 ]; then
            echo "   Press Enter to confirm, Ctrl-C to abort." >&2
            read -r
        else
            echo "   Running non-interactively; pass --yes to proceed." >&2
            echo "   Aborting." >&2
            exit 1
        fi
    fi
    find "$TRACES_DIR" -mindepth 1 -maxdepth 1 \
        \( -name '*.jsonl' -o -name '*.txt' -o -name '*.json' \) \
        -delete
    echo "   traces/ cleared." >&2
fi

echo "amplify runner (Pass 1: calibration)" >&2
echo "  inputs: $INPUTS_FILE" >&2
echo "  trials per problem: $TRIALS" >&2
[ -n "$RUNG_FILTER" ] && echo "  rung filter: $RUNG_FILTER" >&2
echo "  cache mode: ${CACHE_MODE:-first-run}" >&2
echo "  total runs planned: $TOTAL_RUNS" >&2
echo "  output directory: $TRACES_DIR" >&2
echo "" >&2

# Confirm before launching expensive runs.
if [ "$TOTAL_RUNS" -gt 50 ] && [ -z "$YES" ]; then
    echo "WARNING: $TOTAL_RUNS agent invocations is a lot of tokens." >&2
    if [ -t 0 ]; then
        echo "Press Enter to continue, Ctrl-C to abort." >&2
        read -r
    else
        echo "Running non-interactively; pass --yes to proceed." >&2
        exit 1
    fi
fi

# Verify claude is available.
if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' CLI not found in PATH." >&2
    echo "Install Claude Code or update this script's invocation to match your harness." >&2
    exit 1
fi

run_count=0
fail_count=0

# Track consecutive incomplete runs. If the runner sees CASCADE_THRESHOLD
# of them in a row, it aborts the whole batch on the assumption that
# something systemic is wrong.
consecutive_incomplete=0
CASCADE_THRESHOLD=3

# Model: Haiku 4.5 — chosen for cost. Override with MODEL=claude-sonnet-4-6
# or MODEL=claude-opus-4-6 for cross-model comparisons. Note: changing the
# model changes `p`, so calibration data does not transfer across models.
MODEL="${MODEL:-claude-haiku-4-5-20251001}"

# Read PAIRS via process substitution rather than `echo "$PAIRS" | while …`.
# Piping into `while` runs the loop body in a subshell, which (a) discards
# any state mutations (run_count, fail_count, consecutive_incomplete) when
# the loop exits, and (b) makes `exit 2` from the cascade-abort branch
# only kill the subshell, not the script. Process substitution keeps the
# loop in the parent shell so counters persist and `exit 2` aborts the
# whole runner the way we want.
while IFS=$'\t' read -r rung label problem_index a b; do
    for trial in $(seq 1 "$TRIALS"); do
        run_count=$((run_count + 1))
        run_id="rung${rung}-${label}-p${problem_index}-t$(printf '%03d' "$trial")"
        trace_file="$TRACES_DIR/${run_id}.jsonl"
        conv_file="${TRACES_DIR}/${run_id}.conversation.jsonl"
        stderr_file="${TRACES_DIR}/${run_id}.agent-stderr.txt"

        # Cache check.
        if [ -s "$trace_file" ] && grep -qE "$RUN_END_PATTERN" "$trace_file"; then
            echo "[$run_count/$TOTAL_RUNS] $run_id (cached, skipping)" >&2
            continue
        fi

        : > "$trace_file"

        prompt="Compute a × b where a=${a} and b=${b}. Run id: ${run_id}. Trace file: ${trace_file}. Begin."

        echo "[$run_count/$TOTAL_RUNS] $run_id (a=$a, b=$b)" >&2

        (
            cd "$ROOT"
            claude \
                --print "$prompt" \
                --model "$MODEL" \
                --output-format stream-json \
                --verbose \
                --max-turns 30 \
                --permission-mode bypassPermissions \
                < /dev/null \
                > "$conv_file" \
                2> "$stderr_file"
        ) || {
            fail_count=$((fail_count + 1))
            echo "  -> claude CLI exited nonzero (see ${run_id}.agent-stderr.txt)" >&2
        }

        # Post-run validation.
        if [ -s "$trace_file" ] && grep -qE "$RUN_END_PATTERN" "$trace_file"; then
            consecutive_incomplete=0
        else
            echo "  -> INCOMPLETE (no run_end event). Removing partial files;" >&2
            echo "     next invocation will retry this run." >&2
            rm -f "$trace_file" "$conv_file" "$stderr_file"
            fail_count=$((fail_count + 1))
            consecutive_incomplete=$((consecutive_incomplete + 1))
            if [ "$consecutive_incomplete" -ge "$CASCADE_THRESHOLD" ]; then
                echo "" >&2
                echo "  ⚠  $CASCADE_THRESHOLD consecutive runs failed without a run_end." >&2
                echo "     Aborting the rest of this batch after $run_count/$TOTAL_RUNS runs ($fail_count failed)." >&2
                echo "     Re-run ./runner/run.sh after investigating." >&2
                exit 2
            fi
        fi
    done
done < <(printf '%s\n' "$PAIRS")

echo "" >&2
echo "Done. $run_count/$TOTAL_RUNS runs attempted, $fail_count failed." >&2
echo "Traces written to $TRACES_DIR/" >&2
echo "Next: ./verify/trace_verify_all.sh" >&2
