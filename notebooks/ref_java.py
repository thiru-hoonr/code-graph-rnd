"""Reference parser for Java: "functions expected" from javac's own parser.

Same contract as ref_python.py. The work is done by java-ref/ListMethods.java, run with the JDK's
source launcher (`java ListMethods.java`, JDK 11+). Parse only — nothing is compiled, so no
classpath or project dependencies are needed. What counts (methods, constructors, compact record
constructors, annotation members) and what is unnamed (lambdas) is listed at the top of that file.

Needs a JDK on PATH (`java`, with the jdk.compiler module). Without one this raises with a clear
message instead of producing numbers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

LANGUAGE = "java"  # code-review-graph's name for .java
LABEL = "javac parser (JDK, java-ref)"
SCRIPT = Path(__file__).resolve().parent.parent / "java-ref" / "ListMethods.java"


def _java() -> str:
    """The JDK's `java`: $JAVA_HOME/bin/java if set, else `java` on PATH. macOS ships a /usr/bin/java
    stub that fails when no JDK is registered, so each candidate is probed, not just found."""
    import os  # noqa: PLC0415
    home = os.environ.get("JAVA_HOME")
    for java in ([str(Path(home) / "bin" / "java")] if home else []) + [shutil.which("java") or ""]:
        if java and subprocess.run([java, "-version"], capture_output=True, text=True).returncode == 0:
            return java
    raise RuntimeError("no working JDK: set JAVA_HOME to the JDK, or make `java` on PATH a real JDK "
                       "(macOS: /usr/bin/java is only a stub until a JDK is registered)")


def expected(root: Path, files: list[str]) -> dict[str, dict]:
    """Named methods per file. File list goes on stdin (thousands of files would overflow argv).
    A file with syntax errors is listed but flagged: javac's parser recovers, so it may have
    missed methods inside the broken part."""
    if not files:
        return {}
    java = _java()
    print("$ java", SCRIPT.name, root.name, f"({len(files)} files on stdin)")
    done = subprocess.run([java, str(SCRIPT), str(root)], input="\n".join(files),
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"ListMethods.java exited {done.returncode}: {done.stderr[-2000:]}")
    out: dict[str, dict] = {}
    for rel, r in json.loads(done.stdout).items():
        if "error" in r:
            out[rel] = {"named": [], "unnamed": [], "error": f"javac: {r['error']}"}
            continue
        out[rel] = {"named": [(f["name"], f["line"], f["end"]) for f in r["named"]],
                    "unnamed": [(f["line"], f["end"]) for f in r["unnamed"]],
                    "error": (f"javac: {r['syntax_errors']} syntax error(s), result may be incomplete"
                              if r["syntax_errors"] else None)}
    return out
