#!/usr/bin/env python3
"""Grader for recurse.

Usage:
    pixi run python verify/grade.py "<lambda source>" <a> <b> <c> <m> <x_domain_max>

Parses the lambda source under a strict AST whitelist (no function
calls, no imports, no names besides x), evaluates it on every integer
in [0, x_domain_max], and compares against the ground-truth function
((a*x + b) ^ c) % m. Prints exactly one line:

    RESULT: PASS
    RESULT: FAIL <reason>

and exits 0 / 1 accordingly. The parent agent runs this from the
source tree as the deterministic boundary between the recursive loop
and the answer; the child copy in /tmp does not have this file.
"""

from __future__ import annotations

import ast
import sys


ALLOWED_AST_NODES = {
    ast.Expression,
    ast.Lambda,
    ast.arguments,
    ast.arg,
    ast.BinOp,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Mod,
    ast.BitXor,
    ast.BitAnd,
    ast.BitOr,
    ast.LShift,
    ast.RShift,
    ast.Constant,
    ast.Name,
    ast.Load,
}


def parse_lambda(text: str):
    text = text.strip()
    if not text.startswith("lambda"):
        return None, "answer must start with 'lambda'"
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        return None, f"not valid Python expression: {e}"
    for node in ast.walk(tree):
        if type(node) not in ALLOWED_AST_NODES:
            return None, f"forbidden AST node {type(node).__name__}"
        if isinstance(node, ast.Name) and node.id != "x":
            return None, f"forbidden name {node.id!r}"
        if isinstance(node, ast.Constant) and not isinstance(node.value, int):
            return None, f"non-integer constant {node.value!r}"
    try:
        fn = eval(  # noqa: S307 — restricted by whitelist above
            compile(tree, "<answer>", "eval"),
            {"__builtins__": {}},
            {},
        )
    except Exception as e:
        return None, f"compile failed: {e}"
    return fn, ""


def main(argv: list) -> int:
    if len(argv) != 7:
        print(
            "RESULT: FAIL usage: grade.py '<lambda>' <a> <b> <c> <m> <x_domain_max>",
            flush=True,
        )
        return 1
    src, a_s, b_s, c_s, m_s, xmax_s = argv[1:]
    try:
        a, b, c, m, xmax = int(a_s), int(b_s), int(c_s), int(m_s), int(xmax_s)
    except ValueError as e:
        print(f"RESULT: FAIL bad numeric arg: {e}", flush=True)
        return 1

    fn, err = parse_lambda(src)
    if fn is None:
        print(f"RESULT: FAIL {err}", flush=True)
        return 1

    for x in range(0, xmax + 1):
        expected = ((a * x + b) ^ c) % m
        try:
            got = fn(x)
        except Exception as e:
            print(f"RESULT: FAIL eval error at x={x}: {e}", flush=True)
            return 1
        if got != expected:
            print(
                f"RESULT: FAIL wrong at x={x}: got {got}, expected {expected}",
                flush=True,
            )
            return 1

    print("RESULT: PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
