# How our code graph works — overview (2026-11-17)

Start here. Details: `A-our-wrapper.md` (Synthetix side, `origin/feature/atlas` @ bd7a82cbc) and
`B-code-review-graph-tool.md` (inside the tool, 2.3.8).

## End to end

```
repo checkout
  → [Synthetix] knowledge/adapters/crg.py + _process.py   (the only code that runs the tool)
       repos → unregister root → build --repo <root> -q → embed --repo <root>
       CRG_DATA_DIR = {project}/.knowledge/{estate}; generated .code-review-graphignore
  → [tool] code-review-graph: tree-sitter parse per file → graph.db (SQLite)
       nodes: File · Class · Function · Test        (no Method kind; interface/struct = Class)
       edges: CONTAINS · CALLS · IMPORTS_FROM · INHERITS · IMPLEMENTS · TESTED_BY · REFERENCES
  → [Synthetix] read graph.db directly (read-only SQLite)
       Discover: graph_units.py — units fallback, structure facts
       Atlas:    code_graph.py · code_sync.py · code_cite.py · code_job.py — code map, Ask citations,
                 fact↔code citations, blast radius (CALLS), tests (TESTED_BY)
```

Triggers: the Index button, and Atlas after each Build. Discover never indexes by itself (SYN-F-053).

## What decides language coverage

The tool does. Our only filter is the ignore file (sensitive paths). So "which languages can we
parse" = the tool's extension map (`parser.py:715`) + the grammars that ship.

| Group | Languages |
|---|---|
| Mapped and parsed | Python, JS/JSX, TS/TSX, Java, Go, C#, Kotlin (`.kt`), Rust, PHP, Ruby, Swift, Dart, C/C++, Scala, … (see B) |
| Not read at all | COBOL (grammar ships, no extension mapped), VB6 (`.bas/.cls/.frm`), `.kts`, `.mm` |

## How good are the relationships (what the source says)

- **CALLS:** a call target is linked to a node only if exactly one candidate is in the same file
  or an imported file; otherwise it stays a bare name. `new X()` gives no call in JS/C#.
- **IMPORTS:** Python relative imports lose the module; `import a.b as c` gives no edge.
- **INHERITS:** the target is never linked to a node — always the base's text. Covers both
  extends and implements. None for Go, Ruby, Objective-C; noisy for Kotlin.
- **Ids** are `absolute_path::Parent.name` — a graph only works on the machine that built it.

## What the notebooks measured (same build as the product)

| App | Source files with nodes | CALLS resolved | IMPORTS resolved | INHERITS resolved |
|---|---|---|---|---|
| crm-prototype (JS) | 89/89 | 18.4% | 73.0% | — (no edges) |
| crm-prototype-client (JSX) | 157/157 | 31.7% | 54.8% | — (no edges) |
| voice-assessment-agent (Py) | 72/72 | 17.0% | 36.4% | 0% of 56 |
| spring-petclinic (Java) | 50/50 (+7/7 sql) | 5.7% | 1.9% (in-repo: 0 of 14) | 0% of 14 (8 are in-repo classes) |

Java is the weakest so far: the graph knows every file and class, but almost none of the links
between them (SYN-F-558). It also adds Spring edges: HANDLES (routes), INJECTS, DEPENDS_ON_CONFIG.

"Resolved" is not "correct": calls into libraries (`console.log`, `res.json`) correctly have no
node. Telling missed links from correct non-links needs the answer key (on hold).

## Findings logged (`~/.claude/synthetix-findings.md`)

Earlier: SYN-F-023, 372, 373, 374, 379, 442. New today: SYN-F-552 (INHERITS never resolves),
553 (absolute paths sent to the browser and an LLM prompt), 554 (code-map edges are a table-order
sample), 555 (Atlas sync can read a graph mid-build, suspected), 556 (`ask()`/answerer/
`check_code_symbol` have no production caller), 557 (Atlas queries the tool's schema directly,
bypassing the adapter), 558 (Java in-repo imports and base classes unresolved).

## Next

1. Team decides how to score → write answer keys per app → score section in each notebook.
2. Grow from 4 apps (JS, JSX, Python, Java) to 6–7: e.g. Go, C#, TypeScript, a legacy stack.
3. Pick the other tools to compare, run them through the same notebook shape.
