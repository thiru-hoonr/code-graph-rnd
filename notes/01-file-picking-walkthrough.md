# Walkthrough 1 — how code-review-graph decides which files it reads

Read directly from the installed package, version 2.3.8, at
`~/.local/share/uv/tools/code-review-graph/lib/python3.12/site-packages/code_review_graph/`.
Every statement carries the file and line it comes from. Covers the path a file takes from
`code-review-graph build` up to the moment its text is handed to the language parser.
What happens inside the parser per language is Walkthrough 2 (not written yet).

## The path of one `build`

| # | Step | Where | What happens |
|---|---|---|---|
| 1 | Command | `cli.py:1754-1783` | `build` calls `build_or_update_graph(full_rebuild=True, …)`. With `-q` (how Synthetix runs it) INFO logs are switched off and the summary line and the error count are **not printed** (`cli.py:1777-1783`). |
| 2 | Entry | `tools/build.py:518-519` | Calls `full_build(root, store)`. |
| 3 | List candidates | `incremental.py:1043-1048` | `git ls-files` (`:982`). If git returns nothing (not a repo, git missing, or timeout after 30 s, `:672`, `:996-997`) it falls back to every file on disk (`rglob("*")`, `:1048`). |
| 4 | Filter | `incremental.py:1050-1071` | Seven checks, in this order. A file failing any check is dropped with no record (two of them log at debug level only). See the table below. |
| 5 | Read | `incremental.py:1231` / `:1286` | `read_bytes()` — the **whole file**, no size limit, no cut. |
| 6 | Parse | `parser.py:2600-2791` | `parse_bytes`. Picks the language again, then either a special parser or tree-sitter. |
| 7 | Store | `graph.py:359-374` | Deletes the file's old rows, inserts its nodes and edges. |
| 8 | Count | `incremental.py:1349` | `files_parsed = len(files)` — the number of files that **passed the filter**, not the number that parsed. |

## The seven filter checks (step 4)

| Order | Check | Line | Dropped when |
|---|---|---|---|
| 1 | Ignore patterns | `incremental.py:1051` | Path matches the built-in list, the repo's `.code-review-graphignore`, or a detected nested build folder |
| 2 | Path can be built | `:1055-1059` | The OS refuses the path |
| 3 | Path length | `:1060-1062` | Full path over 1,000 characters, or any one folder/file name over 255 bytes |
| 4 | Is a file | `:1063` | Not a regular file (e.g. listed by git but deleted on disk) |
| 5 | Not a symlink | `:1065` | It is a symbolic link |
| 6 | Language known | `:1067` | `detect_language` returns nothing (see below) |
| 7 | Not binary | `:1069`, `:663-669` | A zero byte in the first 8,192 bytes, **or the file cannot be read** (an unreadable file is treated as binary) |

## How the language is decided (`parser.py:2485-2519`)

1. Name ends in `.blade.php` → `blade` (`:2501`).
2. Otherwise the file's extension, lower-cased, is looked up in the extension map
   (`parser.py:715-785`, 62 extensions).
3. `.yml`/`.yaml` becomes `ansible` or `spring_config` if the path looks like one (`:2505-2508`).
4. `.properties` is dropped unless it is a Spring config file (`:2509-2510`).
5. A file with **no extension at all** gets a 256-byte look at its first line for a `#!`
   interpreter (`:2516-2518`, `:849`); 18 interpreters are known (`:821-844`).
6. Anything else → no language → the file is dropped at filter check 6.

A repo can add languages in `.code-review-graph/languages.toml` (`parser.py:2443-2467`): an
extension mapped onto a grammar that already ships. Synthetix does not write this file.

## What happens inside `parse_bytes` before the real parsing (`parser.py:2600-2705`)

| Language | Line | Route |
|---|---|---|
| blade, vue, svelte, notebook | `:2629-2642` | own parser each |
| Python starting `# Databricks notebook source` | `:2649-2657` | notebook parser |
| vbnet (`.vb`), rescript | `:2661-2666` | regex-based, no grammar |
| sql | `:2670-2671` | tree-sitter + regex for `CREATE PROCEDURE` |
| ansible, spring_config | `:2674-2693` | own parser; **returns nothing if the `yaml` library is missing** |
| generic yaml | `:2696-2697` | **returns nothing, always** |
| everything else | `:2699-2705` | tree-sitter grammar; **returns nothing if the grammar fails to load** (`:2701-2702`) |

Then `parser.parse(source)` (`:2705`). Tree-sitter always returns a tree, even for a file full
of syntax errors. **The code never checks the tree for errors** — there is no `has_error` or
`ERROR`-node check on this path (the only `ERROR` checks, `:2799-2805`, decide whether a `.h` is
C or C++). So a half-broken file is stored exactly like a clean one.

## Hard-coded limits and skip rules found so far

| # | Rule | Value | Where | Can be changed by | Maps to the sheet's "Why" |
|---|---|---|---|---|---|
| H1 | Built-in ignore list | 39 patterns: `node_modules`, `vendor`, `.venv`, `venv`, root `dist` `build` `target` `bin` `obj` `storage` `coverage` `tmp`, `*.min.js`, `*.min.css`, `*.map`, `*.lock`, `*.jar`, `*.db`, … | `incremental.py:164-210` | adding to it via `.code-review-graphignore`; **cannot remove a built-in** (`:479`, `:475-477`) | Third-party / vendored · Generated file · Skipped on purpose |
| H2 | Nested build folders | `target`, `build`, `.next`, `.nuxt` beside a build manifest; scan 3 levels deep, 2,000 folders, 200 patterns | `incremental.py:218-242` | env `CRG_NESTED_OUTPUT_SCAN=0`, `!path` in ignore file | Generated file |
| H3 | Only git-tracked files | untracked files are never seen | `incremental.py:982`, `:1043-1045` | — | Skipped on purpose |
| H4 | Git timeout | 30 s, then falls back to **all files on disk** | `incremental.py:672`, `:996`, `:1048` | env `CRG_GIT_TIMEOUT` | — (changes the file list) |
| H5 | Submodules | not included | `incremental.py:677`, `:983` | env `CRG_RECURSE_SUBMODULES` | Skipped on purpose |
| H6 | Path length | 1,000 chars total, 255 bytes per name | `incremental.py:1060` | — | Other |
| H7 | Symlinks | skipped | `incremental.py:1065` | — | Skipped on purpose |
| H8 | Extension map | 62 extensions; anything else has no language | `parser.py:715-785` | `languages.toml` | Language not supported |
| H9 | `.properties` | dropped unless Spring config | `parser.py:2509-2510` | — | Language dropped |
| H10 | Shebang probe | first 256 bytes, 18 interpreters, extension-less files only | `parser.py:821-849`, `:2516` | — | Language not supported |
| H11 | Binary test | zero byte in first 8,192 bytes; unreadable = binary | `incremental.py:663-669` | — | Binary / compiled |
| H12 | Generic YAML | accepted by the filter, parser returns nothing | `parser.py:2696-2697` | — | Language dropped |
| H13 | Grammar not loadable | parser returns nothing, warning only | `parser.py:2701-2702`, `:542-545` | — | Language not supported |
| H14 | Tree depth | below 180 levels of nesting, the walk stops silently | `parser.py:5554`, `:6266-6267` | — | Too big (cut off) |
| H15 | Parse exception | file gets no rows; logged as a warning, added to `errors` | `incremental.py:1241-1242`, `:1315-1320` | — | Parse error |
| H16 | Parallel threshold | under 8 files → one by one; else up to 8 workers | `incremental.py:28`, `:1281` | env `CRG_PARSE_WORKERS`, `CRG_SERIAL_PARSE` | — (speed only) |

Not found on this path: **no file-size limit and no character or line cut**. The whole file is
read (`incremental.py:1231`, `:1286`). The "cut at 60,000 characters" in the team's example row
is not a code-review-graph rule.

## Three things that affect how the sheet must be filled

1. **The tool's own "files parsed" number cannot be used.** It is the count of files that passed
   the filter (`incremental.py:1349`). It includes files whose parse threw an error and files
   the parser returned nothing for (YAML). Proof from a build already done: voice-assessment-agent
   reported 77 files; the repo has 72 `.py` + 2 `.sh` + 3 `.yml` = 77, and the 3 YAML files have
   no rows in the graph. "Files parsed" in sheet 2 has to be counted from `graph.db`
   (files that have a `File` node), not taken from the tool's message.
2. **The tool has no "partly read".** It never records syntax errors, so it cannot say a file
   was half understood. To fill "Partly read" and "Lines read" we must re-parse each file with the
   same grammar ourselves and measure the lines covered by tree-sitter `ERROR` nodes, plus any
   file deeper than 180 levels (H14).
3. **"Shown to user?" is "No" for every drop above, inside the tool.** Filter drops leave no
   trace; two log at debug level; parse errors log a warning, and with `-q` the error count is
   not printed (`cli.py:1777-1783`). Whether Synthetix shows any of it is decided in our wrapper
   (Walkthrough 3), still to be read.

## Not yet read

- `parser.py` beyond line 2791: how classes, functions, imports and calls are pulled out of the
  tree, per language (Walkthrough 2).
- `incremental_update` (`incremental.py:1364`): not used — Synthetix always runs a full build.
- Synthetix's wrapper files on `feature/atlas` (Walkthrough 3).
