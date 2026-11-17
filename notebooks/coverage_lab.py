"""Fill the team's Ingestion_Coverage.xlsx for one repo, for code-review-graph 2.3.8.

Every rule here is the tool's own code, imported from the same version Synthetix pins, so a
"why" is the tool's reason, not a guess. Line references point at the installed package
(see notes/01-file-picking-walkthrough.md and notes/02-what-counts-as-a-function.md).

Definitions (agreed 2026-11-17):
- Lines: lines in the file (``len(text.splitlines())``).
- Fully read: the tool stored the file (it has a File row) and tree-sitter found no syntax error.
- Partly read: stored, but some lines sit inside tree-sitter ERROR/MISSING nodes, or the tree is
  deeper than the tool's 180-level walk limit. Lines read = lines minus those lines.
- Not read: the tool dropped the file before parsing, or parsed it and stored nothing.
- Functions expected: named functions per the language's own parser (Python: ast FunctionDef +
  AsyncFunctionDef). Callbacks without a name are not expected; they are reported separately.
- Functions found: an expected function that has a Function or Test row with the same name and
  start line in the same file.
- Which files are in sheet 1 (Thiru, 2026-11-17): code files only. A file is left out when the
  tool has no language for it AND its extension is not a programming language (docs, licences,
  images, Dockerfiles, config). A programming-language file the tool cannot read (.cbl, .bas,
  .kts, …) stays in, as "Not read / Language not supported". Left-out files are listed separately.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from code_review_graph import incremental as crg_inc
from code_review_graph.parser import CodeParser
from openpyxl import load_workbook
from tree_sitter_language_pack import get_parser

LAB = Path(__file__).resolve().parent.parent
APPS, RESULTS, TEMPLATE = LAB / "apps", LAB / "results", LAB / "templates" / "Ingestion_Coverage.xlsx"
TOOL = shutil.which("code-review-graph", path=str(Path.home() / ".local/bin")) or "code-review-graph"
PACKAGE = "code-review-graph 2.3.8"
MAX_AST_DEPTH = 180  # parser.py:5554
TS_PARSED_SPECIALLY = {"vue", "svelte", "notebook", "blade", "sql", "vbnet", "rescript",
                       "yaml", "ansible", "spring_config"}  # parser.py:2629-2697

# Which value of the sheet's "Why" list each ignore pattern maps to (incremental.py:164-210).
VENDORED = ("node_modules", "vendor", ".venv", "venv", ".bundle", ".gradle", ".dart_tool",
            ".pub-cache", "*.jar")
GENERATED = ("dist", "build", ".next", ".nuxt", "target", "bin", "obj", "coverage", "cdk.out",
             "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "yarn.lock",
             "__pycache__", "*.pyc", "bootstrap/cache", "public/build")


# Programming-language extensions, whether or not the tool supports them. A file with one of these
# stays in sheet 1 even when the tool cannot read it, so an unsupported language shows up as a gap.
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte", ".java", ".kt", ".kts",
    ".scala", ".groovy", ".go", ".rs", ".cs", ".vb", ".fs", ".php", ".rb", ".swift", ".m", ".mm",
    ".dart", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".lua", ".pl", ".pm", ".r", ".jl",
    ".ex", ".exs", ".erl", ".hs", ".clj", ".elm", ".zig", ".nim", ".sol", ".sql", ".sh", ".bash",
    ".zsh", ".ps1", ".cbl", ".cob", ".cpy", ".bas", ".cls", ".frm", ".pas", ".f90", ".asm", ".abap",
}


def is_out_of_scope(rel: str, parser: CodeParser, root: Path) -> bool:
    """True for a file that is not code: the tool has no language for it and its extension is not
    a programming language. Such files are left out of sheet 1 (listed separately)."""
    full = root / rel
    if Path(rel).suffix.lower() in CODE_EXTENSIONS:
        return False
    try:
        return parser.detect_language(full) is None
    except OSError:
        return True


def sh(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """Run a command, echo it, return stdout; raise on failure."""
    print("$", " ".join(cmd), f"(env {env})" if env else "")
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False,
                          env={**os.environ, **env} if env else None)
    if done.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {done.returncode}: {done.stderr[-2000:]}")
    return done.stdout.strip()


# ---------------------------------------------------------------- 1. build, the product's way

def next_run_id(app: str) -> str:
    """The next iteration's folder name: iter-<NNN>_<date>_<time>. Runs are numbered per repo, never reused."""
    runs = RESULTS / app / "runs"
    taken = [int(p.name[5:8]) for p in runs.glob("iter-[0-9][0-9][0-9]*")] if runs.exists() else []
    return f"iter-{max(taken, default=0) + 1:03d}_{dt.datetime.now():%Y-%m-%d_%H%M}"


def run_dir(app: str, run: str) -> Path:
    """results/<app>/runs/<run>/ — everything one iteration produces. Created if missing."""
    path = RESULTS / app / "runs" / run
    path.mkdir(parents=True, exist_ok=True)
    return path


def build(app: str, out: Path) -> Path:
    """Unregister, then `build --repo <root> -q` with CRG_DATA_DIR — as knowledge/adapters/_process.py.

    The graph goes to <out>/crg. A folder already there is refused, never overwritten: each run
    builds into its own iteration folder.
    """
    root, data_dir = APPS / app, out / "crg"
    if data_dir.exists():
        raise FileExistsError(f"{data_dir} already exists — this iteration was already built; start a new run")
    data_dir.mkdir(parents=True)
    if str(root) in sh([TOOL, "repos"]):
        sh([TOOL, "unregister", str(root)])
    sh([TOOL, "build", "--repo", str(root), "-q"], env={"CRG_DATA_DIR": str(data_dir)})
    return data_dir / "graph.db"


def graph_rows(db: Path, root: Path) -> tuple[set[str], pd.DataFrame]:
    """Files with a File row, and every Function/Test row (relative path, name, start line)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    prefix = f"{root.resolve()}/"
    files = {p.removeprefix(prefix) for (p,) in con.execute("select file_path from nodes where kind='File'")}
    funcs = pd.read_sql_query(
        "select file_path, name, line_start, kind from nodes where kind in ('Function','Test')", con)
    funcs["file"] = funcs.file_path.str.removeprefix(prefix)
    return files, funcs[["file", "name", "line_start", "kind"]]


# ---------------------------------------------------------------- 2. why a file was not read

def drop_reason(root: Path, rel: str, parser: CodeParser, patterns: list[str]) -> tuple[str, str] | None:
    """The tool's own filter (incremental.py:1050-1071), in its order. None = the file passes."""
    for pattern in patterns:
        if crg_inc._should_ignore(rel, [pattern]):
            key = pattern.strip("/*")
            why = ("Third-party / vendored" if any(v.strip("*") in pattern for v in VENDORED)
                   else "Generated file" if any(g.strip("*") in pattern for g in GENERATED)
                   else "Skipped on purpose")
            return why, f"ignore pattern {pattern!r} (incremental.py:164-210) [{key}]"
    full = root / rel
    if len(str(full)) > 1000 or any(len(p.encode()) > 255 for p in full.parts):
        return "Other", "path longer than 1000 chars or a name over 255 bytes (incremental.py:1060)"
    if not full.is_file():
        return "Other", "not a regular file on disk (incremental.py:1063)"
    if full.is_symlink():
        return "Skipped on purpose", "symlink (incremental.py:1065)"
    if parser.detect_language(full) is None:
        ext = full.suffix or "(no extension)"
        if full.suffix.lower() == ".properties":
            return "Language dropped", ".properties that is not Spring config (parser.py:2509)"
        return "Language not supported", f"extension {ext} not in the tool's map (parser.py:715-785)"
    if crg_inc._is_binary(full):
        return "Binary / compiled", "zero byte in first 8,192 bytes (incremental.py:663-669)"
    return None


# ---------------------------------------------------------------- 3. syntax errors and depth

def error_lines(language: str, raw: bytes) -> tuple[set[int], int]:
    """Lines inside tree-sitter ERROR or MISSING nodes, and the tree's maximum depth.

    Uses the same grammar the tool loads (parser.py:2700-2705). Languages the tool parses with
    its own code (vue, sql, yaml, …) are not re-checked: returns no lines and depth 0.
    """
    if language in TS_PARSED_SPECIALLY:
        return set(), 0
    tree = get_parser(language).parse(raw)
    bad: set[int] = set()
    deepest = 0
    stack = [(tree.root_node, 0)]
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        if node.type == "ERROR" or node.is_missing:
            bad.update(range(node.start_point[0] + 1, node.end_point[0] + 2))
            continue
        stack.extend((c, depth + 1) for c in node.children)
    return bad, deepest


# ---------------------------------------------------------------- 4. functions expected

# The reference parsers live one per language in ref_<language>.py (see ref_python.py for the
# contract). The notebook decides which ones a project uses and passes them in as `references`:
# a module per language, each with LANGUAGE, LABEL and expected(root, files). Adding a language
# is a new ref_<language>.py file; nothing here changes.


def references_by_language(references: list) -> dict:
    """{language: module} from the reference modules the notebook imported."""
    return {ref.LANGUAGE: ref for ref in references}


# ---------------------------------------------------------------- 5. one row per file

@dataclass
class FileRow:
    repo: str
    file: str
    language: str
    lines: int | None
    read: str
    why: str = ""
    lines_read: int | None = None
    shown: str = ""
    notes: str = ""
    expected: list[tuple[str, int, int]] | None = field(default=None, repr=False)
    callbacks: list[tuple[int, int]] = field(default_factory=list, repr=False)
    expected_error: str | None = field(default=None, repr=False)


def coverage(app: str, db: Path, references: list) -> tuple[list[FileRow], pd.DataFrame, list[str]]:
    """Sheet 1, one row per git-tracked code file; the graph's function rows; the left-out files.

    `references`: the ref_<language> modules this project uses. Sheet 1 covers every language
    regardless; only "functions expected" needs a reference, and only for the languages given.
    """
    root = (APPS / app).resolve()
    parser = CodeParser(root)
    patterns = crg_inc._load_ignore_patterns(root)
    stored, funcs = graph_rows(db, root)
    rows: list[FileRow] = []
    left_out: list[str] = []
    for rel in sh(["git", "ls-files"], cwd=root).splitlines():
        if is_out_of_scope(rel, parser, root):
            left_out.append(rel)
            continue
        full = root / rel
        raw = full.read_bytes() if full.is_file() and not full.is_symlink() else b""
        lines = len(raw.splitlines()) if raw and b"\x00" not in raw[:8192] else None
        language = parser.detect_language(full, raw) or (full.suffix.lstrip(".") or "(none)")
        dropped = drop_reason(root, rel, parser, patterns)
        if dropped:
            rows.append(FileRow(app, rel, language, lines, "Not read", dropped[0], 0, "No", dropped[1]))
            continue
        if rel not in stored:
            why = "Language dropped" if language in ("yaml", "ansible", "spring_config") else "Parse error"
            note = ("passes the filter, parser returns nothing (parser.py:2674-2697)"
                    if why == "Language dropped" else "no File row after build (exception or grammar not loaded)")
            rows.append(FileRow(app, rel, language, lines, "Not read", why, 0, "No", note))
            continue
        bad, depth = error_lines(language, raw)
        notes = []
        if depth > MAX_AST_DEPTH:
            notes.append(f"tree depth {depth} > {MAX_AST_DEPTH}: walk stops (parser.py:6266)")
        if bad:
            notes.append(f"{len(bad)} lines inside syntax errors: {compact(sorted(bad))}")
        partly = bool(bad) or depth > MAX_AST_DEPTH
        row = FileRow(app, rel, language, lines, "Partly read" if partly else "Fully read",
                      ("Parse error" if bad else "Other") if partly else "",
                      (lines or 0) - len(bad), "No" if partly else "", "; ".join(notes))
        rows.append(row)
    # Functions expected: one reference-parser call per language, over the files the tool stored.
    for language, ref in references_by_language(references).items():
        mine = [r for r in rows if r.language == language and r.read != "Not read"]
        results = ref.expected(root, [r.file for r in mine])
        for r in mine:
            found = results[r.file]  # KeyError here = the reference skipped a file: stop, never guess
            r.expected, r.callbacks, r.expected_error = found["named"], found["unnamed"], found["error"]
    return rows, funcs, left_out


def compact(lines: list[int]) -> str:
    """[3,4,5,9] -> '3-5, 9'."""
    out, start = [], None
    for i, n in enumerate(lines):
        if start is None:
            start = n
        if i + 1 == len(lines) or lines[i + 1] != n + 1:
            out.append(f"{start}-{n}" if n != start else f"{n}")
            start = None
    return ", ".join(out)


# ---------------------------------------------------------------- 6. functions found

def match_functions(rows: list[FileRow], funcs: pd.DataFrame) -> pd.DataFrame:
    """One row per expected function: found, or why not.

    Found = the graph has a Function/Test row in the same file, with the same name, whose start
    line falls inside the function's lines (start..end from the reference parser). A range, not
    an exact line, because for `const f = () => {}` the tool records the statement's line
    (parser.py:8676) while a reference may anchor elsewhere on it.
    """
    out = []
    by_file = {f: g for f, g in funcs.groupby("file")}
    for r in rows:
        if r.expected is None:
            continue
        have = by_file.get(r.file, funcs.iloc[0:0])
        for name, start, end in r.expected:
            same_name = have[have.name == name]
            if ((same_name.line_start >= start) & (same_name.line_start <= end)).any():
                status = "found"
            elif len(same_name):
                status = "lost: same name stored once (graph.py:79, :243)"
            else:
                status = "not found"
            out.append({"language": r.language, "file": r.file, "function": name, "line": start, "status": status})
    return pd.DataFrame(out, columns=["language", "file", "function", "line", "status"])


def could_not_check(rows: list[FileRow]) -> pd.DataFrame:
    """Files the reference parser could not fully check — listed, never silently dropped."""
    return pd.DataFrame([{"language": r.language, "file": r.file, "reason": r.expected_error}
                         for r in rows if r.expected_error], columns=["language", "file", "reason"])


def callbacks_table(rows: list[FileRow]) -> pd.DataFrame:
    """Unnamed functions (callbacks, lambdas) per file: not expected, but shown with their lines."""
    return pd.DataFrame([{"language": r.language, "file": r.file, "callbacks": len(r.callbacks),
                          "lines": sum(e - s + 1 for s, e in r.callbacks)}
                         for r in rows if r.callbacks], columns=["language", "file", "callbacks", "lines"])


def parsers_table(app: str, rows: list[FileRow], matched: pd.DataFrame, run: str,
                  references: list) -> pd.DataFrame:
    """Sheet 2, one row per language the tool tried on this repo."""
    df = pd.DataFrame([r.__dict__ for r in rows])
    tried = df[~df.notes.str.contains("not in the tool's map|ignore pattern|zero byte|symlink|regular file|longer", regex=True)]
    out = []
    for lang, g in tried.groupby("language"):
        m = matched[matched.language == lang]
        refs = references_by_language(references)
        has_ref = lang in refs
        reference = refs[lang].LABEL if has_ref else "none"
        lang_rows = [r for r in rows if r.language == lang]
        n_cb = sum(len(r.callbacks) for r in lang_rows)
        cb_lines = sum(e - s + 1 for r in lang_rows for s, e in r.callbacks)
        unchecked = sum(1 for r in lang_rows if r.expected_error)
        out.append({
            "Language": lang, "Package": f"{PACKAGE} (grammar: {lang})", "Repo": app,
            "Files tried": len(g), "Files parsed": int((g.read != "Not read").sum()),
            "Functions expected": len(m) if has_ref else None,
            "Functions found": int((m.status == "found").sum()) if has_ref else None,
            "Evidence": f"results/{app}/runs/{run}/coverage.ipynb",
            "Notes": (f"expected from {reference}; lost to same-name overwrite: "
                      f"{int(m.status.str.startswith('lost').sum())}; not found: {int((m.status == 'not found').sum())}; "
                      f"unnamed functions not counted: {n_cb} ({cb_lines} lines); "
                      f"files the reference could not fully check: {unchecked}"
                      if has_ref else "no reference parser for this language yet"),
        })
    return pd.DataFrame(out)


# ---------------------------------------------------------------- 7. write the team's workbook

def write_workbook(app: str, rows: list[FileRow], parsers: pd.DataFrame, out_dir: Path) -> Path:
    """Copy the template, fill sheets 1-3 for this repo, keep the template's formulas."""
    out = out_dir / f"Ingestion_Coverage_{app}.xlsx"
    wb = load_workbook(TEMPLATE)
    s1, s2, s3 = wb["1. Coverage"], wb["2. Parsers"], wb["3. Summary"]
    # Values are always assigned with `.value = …`: openpyxl's `cell(r, c, None)` does NOT clear a
    # cell, which left the template's example numbers (310 / 301) in a row with no reference parser.
    for r, row in enumerate(rows, start=5):
        values = [row.repo, row.file, row.language, row.lines, row.read, row.why or None,
                  row.lines_read, row.shown or None, f'=IF(OR(D{r}="",D{r}=0),"",G{r}/D{r})',
                  row.notes or None]
        for c, v in enumerate(values, start=1):
            s1.cell(r, c).value = v
    for r, p in enumerate(parsers.to_dict(orient="records"), start=6):
        values = {1: p["Language"], 2: p["Package"], 3: p["Repo"], 4: p["Files tried"],
                  5: p["Files parsed"], 7: p["Functions expected"], 8: p["Functions found"],
                  11: p["Evidence"], 12: p["Notes"],
                  6: f'=IF(OR(D{r}="",D{r}=0),"",E{r}/D{r})',
                  9: f'=IF(OR(G{r}="",G{r}=0),"",H{r}/G{r})',
                  10: f'=IF(A{r}="","",IF(OR(F{r}="",I{r}=""),"Pending",IF(AND(F{r}>=$C$3,I{r}>=$E$3),"Good",IF(AND(F{r}>=$G$3,I{r}>=$I$3),"Partly","Poor"))))'}
        for c, v in values.items():
            s2.cell(r, c).value = None if (not isinstance(v, str) and pd.isna(v)) else v
    s3["A5"] = app
    wb.save(out)
    return out


RUNS_LOG_COLUMNS = ["run", "run_at", "app_commit", "tool", "files", "fully_read", "partly_read",
                    "not_read", "lines", "lines_read", "pct_lines_read", "language",
                    "functions_expected", "functions_found", "notes"]


def log_run(app: str, run: str, stamp: dict[str, str], summary: dict, parsers: pd.DataFrame, notes: str = "") -> Path:
    """Append this iteration to results/<app>/runs.csv — one line per language the tool tried.

    Languages without a reference parser get a line too, with functions left empty, so every run
    is in the log. Append-only: earlier lines are never rewritten.
    """
    path = RESULTS / app / "runs.csv"
    lines = []
    for p in parsers.to_dict(orient="records"):
        has_ref = not pd.isna(p["Functions expected"])
        lines.append({"run": run, "run_at": stamp["run_at"], "app_commit": stamp["app_commit"],
                      "tool": stamp["tool"], "files": summary["Files"], "fully_read": summary["Fully read"],
                      "partly_read": summary["Partly read"], "not_read": summary["Not read"],
                      "lines": summary["Lines"], "lines_read": summary["Lines read"],
                      "pct_lines_read": summary["% lines read"], "language": p["Language"],
                      "functions_expected": int(p["Functions expected"]) if has_ref else None,
                      "functions_found": int(p["Functions found"]) if has_ref else None,
                      "notes": notes or ("" if has_ref else "no reference parser for this language yet")})
    pd.DataFrame(lines, columns=RUNS_LOG_COLUMNS).to_csv(path, mode="a", header=not path.exists(), index=False)
    return path


def run_stamp(app: str) -> dict[str, str]:
    """What this result depends on."""
    return {"app": app, "app_commit": sh(["git", "rev-parse", "--short", "HEAD"], cwd=APPS / app),
            "tool": sh([TOOL, "--version"]), "run_at": dt.datetime.now().isoformat(timespec="seconds")}
