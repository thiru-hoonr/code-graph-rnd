"""Reference parser for JavaScript / JSX: "functions expected" from the TypeScript compiler.

Same contract as ref_python.py. The work is done by js-ref/functions.mjs (TypeScript compiler
5.8.3, pinned in js-ref/package.json); this file calls it once per repo and reshapes the result.
Named patterns 1-8 and what counts as unnamed are listed at the top of functions.mjs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

LANGUAGE = "javascript"  # code-review-graph's name for .js, .jsx and .mjs
LABEL = "TypeScript compiler 5.8.3 (js-ref)"
SCRIPT = Path(__file__).resolve().parent.parent / "js-ref" / "functions.mjs"


def expected(root: Path, files: list[str]) -> dict[str, dict]:
    """Named functions per file. A file with syntax errors is listed but flagged: the compiler
    recovers from errors, so it may have missed functions inside the broken part."""
    if not files:
        return {}
    print("$ node", SCRIPT.name, root.name, f"({len(files)} files)")
    done = subprocess.run(["node", str(SCRIPT), str(root), *files],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"functions.mjs exited {done.returncode}: {done.stderr[-2000:]}")
    out: dict[str, dict] = {}
    for rel, r in json.loads(done.stdout).items():
        if "error" in r:
            out[rel] = {"named": [], "unnamed": [], "error": f"TypeScript compiler: {r['error']}"}
            continue
        out[rel] = {"named": [(f["name"], f["line"], f["end"]) for f in r["named"]],
                    "unnamed": [(f["line"], f["end"]) for f in r["unnamed"]],
                    "error": (f"TypeScript compiler: {r['syntax_errors']} syntax error(s), result may be incomplete"
                              if r["syntax_errors"] else None)}
    return out
