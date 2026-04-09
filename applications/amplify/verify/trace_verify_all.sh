#!/usr/bin/env bash
#
# Run trace_verify.py on every trace in traces/ and print a summary
# grouped by rung. The per-rung pass rate IS the calibration measurement
# of `p` for that rung — that is the entire output of Pass 1.
#
# Implementation: this is a thin shell wrapper around aggregate.py because
# macOS ships bash 3.2 (no associative arrays) and grouping logic in
# bash 3.2 is not worth the complexity.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/verify/aggregate.py" "$ROOT/traces" "$ROOT/verify/trace_verify.py"
