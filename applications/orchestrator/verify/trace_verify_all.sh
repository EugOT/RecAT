#!/usr/bin/env bash
#
# Run trace_verify.py on every trace in traces/ and print a summary.
#
# Output format: one line per trace prefixed with ✓ or ✗, plus aggregate
# pass/fail counts at the end. Failures are listed individually so the
# summary is actionable. A separate informational line counts how many
# successful traces converged in only one round (i.e., the
# tutor/student loop never engaged) — those are still verifier-clean
# but they are weak evidence for the orchestrator-pattern claim.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERIFY="$ROOT/verify/trace_verify.py"
TRACES_DIR="$ROOT/traces"
PYTHON="${PYTHON:-python}"

if [ ! -d "$TRACES_DIR" ]; then
    echo "No traces/ directory. Run ./runner/run.sh first." >&2
    exit 1
fi

shopt -s nullglob
all_jsonl=("$TRACES_DIR"/*.jsonl)
shopt -u nullglob

# Filter out conversation files (*.conversation.jsonl) — those are the
# stream-json logs from invoking claude, not event traces.
traces=()
for f in "${all_jsonl[@]}"; do
    case "$f" in
        *.conversation.jsonl) ;;
        *) traces+=("$f") ;;
    esac
done

if [ "${#traces[@]}" -eq 0 ]; then
    echo "No traces found in $TRACES_DIR. Run ./runner/run.sh first." >&2
    exit 1
fi

pass=0
fail=0
single_round_passes=0
failures=()

for t in "${traces[@]}"; do
    out=$("$PYTHON" "$VERIFY" "$t")
    verdict=${out#RESULT: }
    verdict=${verdict%% *}
    case "$verdict" in
        PASS)
            pass=$((pass + 1))
            # Inspect the trace to see how many rounds it ran. If just 1,
            # the iteration loop never engaged — flag separately.
            rounds=$("$PYTHON" -c "
import json, sys
n = 0
with open('$t') as f:
    for line in f:
        if not line.strip(): continue
        ev = json.loads(line)
        if ev.get('event') == 'round_start':
            n += 1
print(n)
")
            if [ "$rounds" = "1" ]; then
                single_round_passes=$((single_round_passes + 1))
                printf '✓  %s  (1 round, no iteration)\n' "$(basename "$t")"
            else
                printf '✓  %s  (%s rounds)\n' "$(basename "$t")" "$rounds"
            fi
            ;;
        FAIL)
            fail=$((fail + 1))
            failures+=("$out")
            printf '✗  %s\n' "$(basename "$t")"
            ;;
        *)
            fail=$((fail + 1))
            failures+=("unknown: $out")
            printf '?  %s  (%s)\n' "$(basename "$t")" "$out"
            ;;
    esac
done

total=$((pass + fail))
echo ""
echo "Summary: $pass/$total passed verifier, $fail failed"
if [ "$single_round_passes" -gt 0 ]; then
    echo "Note: $single_round_passes pass(es) used only 1 round — the tutor/student loop did not engage on those runs."
fi

if [ "$fail" -gt 0 ]; then
    echo ""
    echo "Failures:"
    for f in "${failures[@]}"; do
        echo "  $f"
    done
    exit 1
fi
