#!/usr/bin/env bash
#
# recurse: thin wrapper that kicks off the parent agent.
#
# Usage:
#   ./run.sh                       # run instance easy-1, fresh
#   ./run.sh easy-2                # run instance easy-2
#   ./run.sh easy-1 --resume       # don't reset state/agent-program.md
#   ./run.sh -h                    # print help
#
# This wrapper does almost nothing. The "loop" lives entirely inside
# .claude/CLAUDE.md as natural-language instructions the parent agent
# follows using its tools (Bash, Read, Write, Edit). There is no
# Python orchestrator. The wrapper just:
#   1. Resets state/agent-program.md from the .initial template (unless --resume).
#   2. Wipes traces/iterations/iter-*.md (unless --resume).
#   3. cd's into the source tree so .claude/CLAUDE.md auto-loads.
#   4. Invokes `claude --print` with a tiny bootstrap prompt that names
#      the instance to attempt.
#   5. Captures the parent's stream-json conversation log.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

usage() {
    cat <<'EOF'
./run.sh [INSTANCE] [--resume]

Run one trial of the recurse parent agent on the named
instance. Writes per-iteration logs to traces/iterations/iter-NNN.md,
the parent's stream-json conversation to traces/parent.conversation.jsonl,
and a single-line verdict to traces/verdict.txt.

Positional:
  INSTANCE      instance id from inputs/inputs.json (default: easy-1)

Options:
  --resume      Do NOT reset state/agent-program.md or wipe
                traces/iterations/. Use this to continue editing the
                child program from where the previous run left off.
  -h, --help    Print this help and exit.

Environment:
  MODEL         override the parent's model (default: claude-haiku-4-5-20251001).
EOF
}

INSTANCE=""
RESUME=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --resume)  RESUME=1; shift ;;
        --) shift; break ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *)
            if [ -z "$INSTANCE" ]; then
                INSTANCE="$1"
            else
                echo "Unexpected positional arg: $1" >&2
                exit 1
            fi
            shift ;;
    esac
done
INSTANCE="${INSTANCE:-easy-1}"

mkdir -p traces/iterations

if [ -z "$RESUME" ]; then
    cp state/agent-program.initial.md state/agent-program.md
    rm -f traces/iterations/iter-*.md traces/iterations/setup.md
    rm -f traces/parent.conversation.jsonl traces/parent.stderr.txt traces/verdict.txt
fi

if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' CLI not found in PATH." >&2
    exit 1
fi

MODEL="${MODEL:-claude-haiku-4-5-20251001}"

echo "recurse" >&2
echo "  instance:  $INSTANCE" >&2
echo "  model:     $MODEL" >&2
echo "  cwd:       $ROOT" >&2
echo "  resume:    ${RESUME:-no}" >&2
echo "" >&2

PROMPT="Begin a recursive self-improvement trial on instance_id=$INSTANCE. Follow the loop in your .claude/CLAUDE.md exactly: read state/agent-program.md, read inputs/inputs.json (find the matching instance and hold its parameters in your context — never write them anywhere persistent), run the iteration loop, and stop when you reach a verdict."

claude --print "$PROMPT" \
    --model "$MODEL" \
    --output-format stream-json \
    --verbose \
    --max-turns 200 \
    --permission-mode bypassPermissions \
    > traces/parent.conversation.jsonl \
    2> traces/parent.stderr.txt

echo "" >&2
echo "Parent done. Inspect:" >&2
echo "  traces/verdict.txt" >&2
echo "  traces/parent.conversation.jsonl" >&2
echo "  traces/iterations/setup.md   traces/iterations/iter-NNN.md" >&2
echo "  state/agent-program.md   (final mutated child program)" >&2
echo "  /tmp/recurse-iter-*/    (per-iteration child workspaces)" >&2
