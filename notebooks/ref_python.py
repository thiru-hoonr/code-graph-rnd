"""Reference parser for Python: "functions expected" from Python's own parser (ast).

Contract shared by every ref_<language>.py file:
    LANGUAGE   the language name code-review-graph uses (parser.py:715-785)
    LABEL      what the sheet's Notes say the expected count came from
    expected(root, files) -> {file: {"named": [(name, start, end)], "unnamed": [(start, end)], "error": str | None}}
        named   = the denominator ("functions expected")
        unnamed = reported, never counted (here: lambdas)
        error   = the reference could not check the file; it is listed, never silently dropped
"""

from __future__ import annotations

import ast
from pathlib import Path

LANGUAGE = "python"
LABEL = "Python ast (stdlib)"


def expected(root: Path, files: list[str]) -> dict[str, dict]:
    """Every def / async def in each file. Lambdas are unnamed, so not expected."""
    out: dict[str, dict] = {}
    for rel in files:
        try:
            tree = ast.parse((root / rel).read_bytes())
        except SyntaxError as e:
            out[rel] = {"named": [], "unnamed": [], "error": f"Python ast: {e.msg} at line {e.lineno}"}
            continue
        out[rel] = {"named": [(n.name, n.lineno, n.end_lineno) for n in ast.walk(tree)
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))],
                    "unnamed": [(n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, ast.Lambda)],
                    "error": None}
    return out
