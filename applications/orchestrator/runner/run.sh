#!/usr/bin/env bash
#
# orchestrator runner.
#
# Implements the orchestrator-pattern tutor/student loop. The shell
# script handles argument parsing, cache mode, and the top-level for-trial
# loop. The per-trial work (the rounds, the claude invocations, the
# grading, the event emission) is delegated to runner/loop.py because
# shell quoting of multi-line student output is too brittle.
#
# Usage:
#   pixi run orchestrator-run                 # use trials_per_run_default
#   pixi run orchestrator-run -- 5            # 5 trials
#   pixi run orchestrator-run -- 5 --fresh    # 5 trials, wipe traces/ first
#   pixi run orchestrator-run -- 5 --resume   # 5 trials, skip complete traces

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INPUTS_FILE="$ROOT/inputs/inputs.json"
TRACES_DIR="$ROOT/traces"
LOOP_PY="$ROOT/runner/loop.py"
PYTHON="${PYTHON:-python}"

mkdir -p "$TRACES_DIR"

# Run this wrapper through Pixi so the harness Python and any child-agent
# `python3` Bash calls resolve inside the reproducible project environment.

# ── Argument parsing ───────────────────────────────────────────────────
TRIALS_ARG=""
CACHE_MODE=""
YES=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --resume) CACHE_MODE="resume"; shift ;;
        --fresh) CACHE_MODE="fresh"; shift ;;
        --yes|-y) YES="1"; shift ;;
        -h|--help)
            cat >&2 <<'EOF'
./runner/run.sh [TRIALS] [--resume | --fresh] [--yes]

Run the orchestrator orchestrator-pattern tutor/student loop. Writes one JSONL
trace per trial to traces/.

Positional:
  TRIALS        independent trials to run (default: trials_per_run_default
                from inputs.json)

Cache control:
  --resume      Skip any trial that already has a complete trace.
  --fresh       Delete every file in traces/ first. Requires --yes when
                non-interactive.
  --yes, -y     Skip confirmation prompts.

Each trial starts from a fresh in-memory copy of templates/student-initial.md so trials are independent.
EOF
            exit 0 ;;
        [0-9]*) TRIALS_ARG="$1"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ── Read inputs.json ───────────────────────────────────────────────────
DEFAULT_TRIALS=$("$PYTHON" -c "import json; print(json.load(open('$INPUTS_FILE'))['trials_per_run_default'])")
TRIALS="${TRIALS_ARG:-$DEFAULT_TRIALS}"

# ── Cache-mode gate ────────────────────────────────────────────────────
RUN_END_PATTERN='"event"[[:space:]]*:[[:space:]]*"run_end"'
has_complete=0
shopt -s nullglob
for f in "$TRACES_DIR"/*.jsonl; do
    case "$f" in *.conversation.jsonl) continue ;; esac
    if [ -s "$f" ] && grep -qE "$RUN_END_PATTERN" "$f"; then
        has_complete=1
        break
    fi
done
shopt -u nullglob

if [ "$has_complete" -eq 1 ] && [ -z "$CACHE_MODE" ]; then
    echo "ERROR: $TRACES_DIR already contains complete traces." >&2
    echo "Pass --resume to skip them or --fresh to wipe them." >&2
    exit 1
fi

if [ "$CACHE_MODE" = "fresh" ]; then
    echo "⚠  --fresh: deleting all files in $TRACES_DIR/" >&2
    if [ -z "$YES" ]; then
        if [ -t 0 ]; then
            echo "   Press Enter to confirm, Ctrl-C to abort." >&2
            read -r
        else
            echo "   Aborting (pass --yes to proceed non-interactively)." >&2
            exit 1
        fi
    fi
    find "$TRACES_DIR" -mindepth 1 -maxdepth 1 -type f ! -name '.gitkeep' -delete
fi

# ── claude CLI sanity ──────────────────────────────────────────────────
if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' CLI not found in PATH." >&2
    exit 1
fi

MODEL="${MODEL:-claude-haiku-4-5-20251001}"

echo "orchestrator runner" >&2
echo "  inputs:           $INPUTS_FILE" >&2
echo "  trials:           $TRIALS" >&2
echo "  model:            $MODEL" >&2
echo "  traces directory: $TRACES_DIR" >&2
echo "" >&2

# ── Trial loop ─────────────────────────────────────────────────────────
for trial in $(seq 1 "$TRIALS"); do
    trial_id=$(printf "trial-%03d" "$trial")
    trace_file="$TRACES_DIR/${trial_id}.jsonl"

    if [ -s "$trace_file" ] && grep -qE "$RUN_END_PATTERN" "$trace_file"; then
        if [ "$CACHE_MODE" = "resume" ]; then
            echo "[$trial/$TRIALS] $trial_id (cached, skipping)" >&2
            continue
        fi
    fi

    : > "$trace_file"

    echo "[$trial/$TRIALS] $trial_id starting" >&2

    # loop.py writes per-invocation conversation logs into TRACES_DIR
    # following the convention <trial_id>.<label>.conversation.jsonl
    # and per-invocation stderr to <trial_id>.<label>.agent-stderr.txt.
    "$PYTHON" "$LOOP_PY" \
        --root "$ROOT" \
        --trial-id "$trial_id" \
        --trace-file "$trace_file" \
        --traces-dir "$TRACES_DIR" \
        --inputs "$INPUTS_FILE" \
        --model "$MODEL"
done

echo "" >&2
echo "Done. Verify with: ./verify/trace_verify_all.sh" >&2
