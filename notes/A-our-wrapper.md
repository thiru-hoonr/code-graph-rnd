# A — How Synthetix wraps and uses its code graph (`code-review-graph` 2.3.8)

Source of truth: branch `origin/feature/atlas` @ `bd7a82cbc` (read with `git show`/`git grep`; working tree untouched).
All `file:line` references are on that commit. Scope: **our** code only; the tool's internals are covered elsewhere.

---

## 0. One-paragraph summary

Synthetix runs `code-review-graph` as a **subprocess** (never imported) from one adapter package,
`src/synthetix/knowledge/adapters/`. Per engagement there are two graphs ("estates"): `source` (the mirrored
legacy checkout) and `workspace` (the tree the build harness writes). Indexing is a queued job claimed by the
worker, which runs `code-review-graph build` then `code-review-graph embed` with `CRG_DATA_DIR` pointing at
`{project}/.knowledge/{estate}/`. Everything after that reads `graph.db` **directly with read-only SQLite**,
except text search, which shells out to `code-review-graph search`. Four product surfaces consume it:
(1) the **Atlas "Code map" tab** (search + a capped, community-coloured canvas), (2) **Atlas fact↔code
citations / blast radius / build & test links**, (3) **Discover** (unit inventory fallback, call-structure
facts in documents and the LLM summary prompt, "where to look" pointer documents), and (4) the
**behaviour ledger** (name-matching ledger rows to destination symbols). Two different abstractions exist:
the official port `knowledge.views.KnowledgeGraph`, and a second, Atlas-only `atlas.code_graph.CodeGraph`
that opens `graph.db` itself — and several Atlas modules query `nodes`/`edges` raw, so the tool's schema is
known well outside the adapter.

---

## 1. How the tool is invoked

### 1.1 Commands actually run (all argv lists, no shell)

| When | Command | Timeout | Where |
|---|---|---|---|
| Before every index | `code-review-graph repos` (stdout scanned for the root string) | 30 s | `knowledge/adapters/_process.py:112-127` |
| ...if the root is listed | `code-review-graph unregister <root>` | 30 s | `_process.py:129-138` |
| Index, step 1 | `code-review-graph build --repo <resolved root> -q` | 3600 s (`TIMEOUT_SEC`) | `adapters/crg.py:88-92`, `_process.py:54` |
| Index, step 2 | `code-review-graph embed --repo <root as given>` (no provider flag ⇒ local model) | 3600 s | `crg.py:108-120` |
| Search | `code-review-graph search <query> --limit N [--repo <root>]`, JSON on stdout | 120 s | `crg.py:157-204` |

- **Never** `update`, `postprocess`, `detect-changes`, `--data-dir`, or any MCP tool. Every index is a full
  `build` (`crg.py:88-89`) even though the port's contract says "idempotent and incremental"
  (`knowledge/views.py:229-235`).
- `--repo` for search: the current tree root wins over the root recorded at index time (`crg.py:171-179`) —
  added after search failed in containers because the tool inferred the root from the worker's CWD `/app`.
- Binary lookup: `shutil.which("code-review-graph")`; missing ⇒ `KnowledgeGraphError` with the install hint
  `uv tool install --with igraph 'code-review-graph[embeddings]==2.3.8'` (`_process.py:64-78`).
- Install: worker image only (`Dockerfile.worker:122-134`, CPU torch index, `UV_TOOL_DIR=/opt/uv-tools` so the
  non-root `worker` user can reach it). The API image deliberately does not carry it (spec 036, see
  `worker/indexing.py:3-7`). Dev-only `just kg-build` → `scripts/knowledge_graph.sh` (82 lines) indexes this repo
  itself with its own ignore list (not used by the product).

### 1.2 Environment

- `env = subprocess_environment() + {"CRG_DATA_DIR": <data_dir>}` (`_process.py:93`).
  `subprocess_environment()` (`config.py:1351-1377`) is `os.environ` minus the platform's own secrets, and
  restores the pre-Atlas `HF_HUB_OFFLINE` value for the child (Atlas toggles it while loading its own model).
- `repos`/`unregister` get `subprocess_environment()` **without** `CRG_DATA_DIR` (`_process.py:125,136`).
- Graph location: `graph_dir = project_root(eid)/.knowledge/{estate}` (`knowledge/paths.py:25-32`) — beside,
  never inside, the estate tree. The Atlas file `atlas.db` lives in the same `.knowledge/` folder
  (`atlas/store.py:89-90`).

### 1.3 Two guarded hazards (`_process.py:8-18`)

1. **Registry redirect.** The tool's registry (keyed by repo root) outranks `CRG_DATA_DIR`; a build under a live
   entry overwrote another graph on 2026-09-08. Mitigation: never pass `--data-dir` (it writes the entry) and
   `unregister` any entry found (`assert_no_registry_entry`, `_process.py:112-138`).
2. **Cloud embeddings.** `--embedding-provider` / `--provider` are asserted absent from every argv
   (`BARRED_PROVIDER_FLAGS`, `_process.py:50,87-92`).

### 1.4 Index sequence (`CrgKnowledgeGraph.index`, `crg.py:64-106`)

1. `root = request.root.resolve()` (macOS `/tmp`→`/private/tmp` symlink problem); must be a dir.
2. `mkdir data_dir`; `assert_no_registry_entry(root)`.
3. If `root/.git` is not a dir, create an **empty** `root/.code-review-graph/` marker dir (the tool refuses a
   root without `.git`/`.svn`/that dir) (`crg.py:79-80`, `_process.py:43-46`).
4. Write `root/.code-review-graphignore` (`write_ignore`, `_process.py:141-151`).
5. Delete the canvas cache `data_dir/slice.json`; write `data_dir/.indexing` (ISO timestamp).
6. `build`, then write `data_dir/estate_root` (the resolved root string, `paths.py:47`), then `embed`.
7. `finally`: remove `.indexing` and the ignore file (the marker dir is left).
8. `stats()`; if `symbols == 0` ⇒ raise "indexed but its graph is empty — refusing to report success".

### 1.5 Ignore file

Content = `# GENERATED by synthetix.knowledge — do not edit.` + `RADIOACTIVE` patterns
(`knowledge/service.py:58-73`):
`/team_data/**`, `/team_data_seed/**`, `/magicbox_real_screens/**`, `/test_data/**`, `/.deliveryos/**`,
`**/settings_state.json`, `*.db`, `*.sqlite`, `*.sqlite3`, `**/.env`, `**/.env.*`, `**/id_rsa`, `**/*.pem`, `**/*.p12`.
Directory patterns are anchored with `/` because an unanchored single-segment `foo/**` matched at any depth and
silently halved an index (measured 2026-09-08, `_process.py:144-148`). That is the **only** filtering applied
at build time; there is no language/extension allowlist in our code — the tool decides what it parses.

### 1.6 Triggering, queueing, timeouts

- `POST /api/engagements/{eid}/knowledge/index` (body `{estate}`) → 409 if the tree is absent, else records a
  row via `IndexQueue.request` and returns 202 (`api/routers/knowledge.py:100-132`, `runs/index_queue.py:27-44`).
- The only other automatic trigger: Atlas's `after_build` requests a **workspace** index after every Build
  (`atlas/build_links.py:96-112`). **Discover never requests an index** (SYN-F-053, still true on this branch;
  `worker/phases/knowledge.py:22-28,214-215` claims archeology "populates" it — it does not).
- The worker claims index rows only when the phase queue is idle (`worker/main.py:324`,
  `worker/indexing.py:47-75`), with a 4,200 s lease (> the tool's 3,600 s timeout, no heartbeat,
  `worker/indexing.py:34-44`); the body runs in a thread (`asyncio.to_thread`, line 88).
- "Indexing" state = `.indexing` marker younger than 3,600 s (`_store.is_indexing`, `_store.py:21-36`);
  older means "interrupted" and the page offers a rebuild.

### 1.7 Error handling

- `run()` raises `KnowledgeGraphError` on non-zero exit (last 2,000 chars of stderr), timeout, or a barred flag
  (`_process.py:81-109`). The worker catches **everything** and records `failed` + the message on the index row
  (`worker/indexing.py:78-101`); the status route shows it as `last_error` (`api/knowledge_view.py:64-65`).
- Search: unparseable JSON ⇒ log `knowledge.search_unparseable`, return `()` (`crg.py:181-185`).
  Router maps `KnowledgeGraphError` to 503 `ERR-KNOWLEDGE-001` (`api/routers/knowledge.py:60,180-181,210-211`).
- SQL reads: opened `file:...?mode=ro`, 5 s timeout; any `sqlite3.DatabaseError` ⇒ warning + `[]`
  (`_store.query`, `_store.py:60-75`). Values coerced with `as_int` so a schema change degrades to 0.
- Every consumer outside the adapter is "never raises": Discover (`discovery/units.py:130-137`,
  `discovery/code_map.py:41-47`), ledger (`core/bel/target.py:158-163`), Atlas (`atlas/code_sync.py:174-181`,
  `code_graph.open_graph` 246-254, `atlas/service.py:612-622`), documents (`discovery/document_retrieval.py:131-136`).

---

## 2. What we read back from `graph.db`

### 2.1 Tables and columns touched

| Table | Columns | Read by |
|---|---|---|
| `nodes` | `name, qualified_name, kind, file_path, line_start, line_end, language` | `crg.symbols` (`crg.py:219-224`), `crg.neighbours` (250-256) |
| `nodes` | `count(*)`, `count(distinct file_path)`, `max(updated_at)`, `language` (group by) | `crg.stats` (128-139) |
| `nodes` | `qualified_name, name, kind, file_path, language, community_id, line_start` | canvas `_slices._compute` (`_slices.py:149-153`) |
| `nodes` | `distinct file_path` | `crg.files` (243); `atlas/code_graph.py:202` (kind='File') |
| `nodes` | `name, file_path, line_start, line_end` where `kind IN ('Function','Class') AND COALESCE(is_test,0)=0` | Atlas `CrgGraph` (`atlas/code_graph.py:197-201`) |
| `nodes` | `qualified_name, line_start, line_end` by `file_path` + line overlap | `atlas/build_links.py:227-232` |
| `nodes` | `qualified_name, file_path` where `is_test = 1 AND name = ?`; `distinct file_path` where `is_test=1` | `atlas/tests_run.py:213, 460` |
| `nodes` | `name, file_path` by `qualified_name` | `atlas/lineage.py:347-349` |
| `edges` | `source_qualified, target_qualified, kind`; `count(*)` | `crg.stats`, `crg.neighbours`, `_slices` (degree + edges) |
| `edges` | `kind='CALLS'` callers of a target (BFS) | `atlas/lineage.py:329-334` |
| `edges` | `kind='TESTED_BY'`, fallback `kind='CALLS'` from a test node | `atlas/tests_run.py:220-231` |
| `embeddings` | `count(*)` only (to decide "searchable") | `crg.py:136`, `views.py:96-104` |
| `communities` | `id, name, size, dominant_language` | `_slices._communities` (`_slices.py:120-134`) |

Search results come from the CLI's JSON (`results[].name/qualified_name/kind/file_path/line_start/line_end/
language`, top-level `search_mode`) — `crg.py:186-203`. We never read `embeddings` vectors or any other table.

### 2.2 Node kinds and how each consumer treats them

The tool's kinds seen in our code: `File`, `Class`, `Function`, plus `Test` / `is_test` and `Type`
(observed via the kind sets below). How we filter:

| Consumer | Kept | Dropped |
|---|---|---|
| `crg.symbols` (Discover denominator, ledger) | everything with `file_path` (`crg.py:222`) | rows with null path |
| `graph_units._kind_of` (`discovery/graph_units.py:78-91,216-223`) | lower-cased kind ∈ `{class, struct, interface, enum, record, trait, object, type, type_alias}` → `data_class`; ∈ `{function, method, constructor, procedure, subroutine, closure, macro}` → `service_fn` | everything else (File, Test, variables, imports) — **counted** as `symbols_not_units` |
| Atlas `CrgGraph` (`atlas/code_graph.py:197-201`) | `Function`, `Class`, non-test, with both line numbers | `File` (used only for the file list), `Test`, `Type`, anything without lines |
| Canvas `_slices` | all kinds, top 1,200 by degree | the rest (`truncated=true`) |
| `build_links._overlapping` | **any** kind whose lines overlap a hunk (no kind filter) | — |
| `tests_run` | test nodes by `is_test=1` | — |

### 2.3 Edge kinds

| Edge kind | Used where | How |
|---|---|---|
| any (`CALLS`, `IMPORTS*`, `INHERITS`, `CONTAINS`, `TESTED_BY`…) | `crg.neighbours` (one hop, both directions, `limit 100`); `_slices` degree + canvas edges; `stats.edges` | kind not filtered; kind string passed through to the wire / documents |
| `CALLS` | `atlas/lineage.py:330` blast radius (callers, BFS to depth, capped); `tests_run.py:228` fallback test→code | filtered by name |
| `TESTED_BY` | `tests_run.py:222` | either direction touching the test node |
| self-loops | `_slices.py:178-179` drops `source = target` | |
| same-file edges | `graph_units.coupling` drops them (`graph_units.py:188-192`) — "a class's own internals" | |

`INHERITS`/`IMPORTS` are never read by name; `CONTAINS` is never filtered out, so it counts toward degree.

### 2.4 Renaming / re-keying

- **Root stripping.** Stored paths and qualified names carry the absolute estate root (`<abs file>::<symbol>`;
  a `File` node's `name` is its absolute path — `knowledge/paths.py:52-62`). The adapter relativises `path`
  (`_store.relative`, falls back to the **basename** if outside the root, `_store.py:78-83`) and removes the root
  string from `name`/`qualified_name` (`paths.strip_root`). `restore_root` re-adds it for `neighbours` lookups
  (`_store.py:86-95`). Root comes from the `estate_root` file, else is guessed by walking up from the first node's
  path to a dir with `.git`/marker (`_store.root_of`, 98-115).
- **Discover unit ids** `{fn|entity}:{path}:{qualified tail}` — the `path::` prefix stripped off the qualified
  name so the file is not doubled (`graph_units.py:237-255`).
- **Atlas evidence keys never use node ids or absolute names** (SYN-F-372): a symbol citation is
  `{estate, path, symbol: name, file_hash: sha256(current text), lines}` (`atlas/code_graph.py:83-90`);
  top-level code is a `code_range` keyed by the sha256 of the block's text (D17). Re-resolution: same path +
  name, else the unique same-named symbol anywhere; ranges by text hash (`code_graph.py:149-167`).
- **Exceptions that do key on `qualified_name`:** `build_links` (`built_as` evidence `{qualified_name,
  tree_digest, lines}`, stored root-stripped, `build_links.py:296-307`), `tests_run` (`tested_by`), `lineage`
  (blast radius re-prefixes the root, `lineage.py:305`). These survive edits but not file moves (SYN-F-372).
- **Ledger** indexes destination symbols by lower-cased `name`, first wins (`core/bel/target.py:166-177`).
- **Search mode** (`semantic`/`keyword`/`none`) is carried to every hit and shown to the reader.

---

## 3. Language / file-type logic in our code

There is **no extension filter around the code graph** in our code — the tool's parser set is the filter.
What exists:

1. **Build-time exclusions** only (§1.5): radioactive dirs, DB files, env/key files.
2. **Discover: language walk first, code map only as fallback** (`discovery/units.py:10-23,110-116`). The
   deterministic, suffix-registry language adapters (`core/oracle/inventory/build.py:187-240`: tally files by
   suffix, pick the dominant registered adapter, e.g. COBOL/Java/PHP/…) run first; the code map is used **only**
   when the walk returns `ok:false` or zero items. Never merged (double-count risk). The provenance field
   `instrument` (`"code-map"` vs empty) tells a reader whether a document came from "a COBOL walk or a
   tree-sitter graph" (`discovery/document_sources.py:281-286`).
3. **Unit kinds** — the allowlist in §2.2; routes, repositories, tables and schemas are **refused** from a
   graph (`graph_units.py:25-40`) because downstream code would treat them as executed/persisted facts.
4. **Test exclusion** — Atlas `CrgGraph` drops `is_test` nodes (`code_graph.py:199`); `tests_run` uses only
   them. `crg.symbols`/`search`/canvas do **not** exclude tests.
5. **Top-level code fallback (D17)** — for files where no symbol matched a fact, Atlas scans
   contiguous non-blank lines not covered by any Function/Class node as "ranges" (`code_graph.py:113-146`).
   This exists because routes, constants and config are top-level statements with no node (SYN-F-442, SYN-F-373).
6. **Literal text scan** (`knowledge/excerpts.py`, `evidence.scan_mentions`) over `graph.files()` — first 400
   files, ≤400 KB each, 6 hits/term; terms must be alphanumeric (drops `snake_case`, paths — SYN-F-377).
   Only reachable from `KnowledgeService.ask()`, which has **no production caller** on this branch (see §6).
7. **Excerpts** read ≤80 lines per symbol, the first 30 lines of each cited file (imports), 400 chars/line,
   60,000 chars total (`excerpts.py:31-47`).
8. **Dominant language** of a code-map envelope = `stats.languages[0]` (alphabetical from the SQL `group by`),
   not the most frequent (`graph_units.py:151`) — see §6.

---

## 4. Data flow

```
 mirror/clone ──► {project}/old/code/<repo>  (estate "source")      Build harness ──► agent workspace (estate "workspace")
        │                                                                   │ atlas.build_links.after_build
        │  POST …/knowledge/index (Index button, Discover/Atlas pages)      │  → IndexQueue.request("workspace")
        ▼                                                                   ▼
   run.index_jobs row ──(worker idle)──► worker/indexing.tick ──► KnowledgeService.index ──► CrgKnowledgeGraph.index
        repos/unregister → write .code-review-graphignore → `build --repo R -q` → write estate_root → `embed --repo R`
                                                  │
                                                  ▼
                     {project}/.knowledge/{estate}/graph.db   (+ estate_root, .indexing, slice.json cache)
                                                  │
   ┌──────────────────────────────┬───────────────┴────────────────┬─────────────────────────────┐
   ▼                              ▼                                ▼                             ▼
 knowledge port (crg.py)      Atlas CrgGraph (code_graph.py)   raw SQL in Atlas             CLI `search`
 stats/symbols/graph/          Function+Class, File list,       build_links (overlap)        crg.search
 neighbours/files              own word-overlap search          lineage (CALLS BFS)           │
   │                              │                              tests_run (TESTED_BY/CALLS)  │
   ▼                              ▼                                │                          ▼
 • Atlas › Code map tab        • code_sync (every Atlas loop     • Atlas lineage / Thing     • Atlas › Code map search
   (KnowledgePage: status,       tick when graph.db mtime          page: built_as, tested_by,  • Atlas Ask code citations
   search, canvas ≤1,200         moves) → re-resolve/retire        blast radius (callers →     (KnowledgeService.citations
   nodes / 6,000 edges)          code citations, judge LLM         facts)                       → excerpts)
 • Discover inventory          • code_cite jobs: fact → ≤10                                   • Discover "where to look"
   fallback (graph_units.units)  candidate spans → judge LLM                                   docs (security / edge
 • Discover structure            → supports / contradicts                                      cases / workflows)
   (coupling: file→file edges,   citations (no code stored,                                   — pointers only, no LLM
   clusters) → HLD, Scope page   only hashes)
   edges, executive-summary    • behaviour-ledger reader links
   LLM prompt                    rules to symbols
 • Ledger destination match
   (core/bel/target, by name)
 • Knowledge phase AKM report (stats only)
```

Surface by surface:

- **Atlas › Code map tab** — `frontend/src/pages/atlas/AtlasPage.tsx:73` mounts `KnowledgePage`
  (`frontend/src/pages/knowledge/KnowledgePage.tsx`), which calls `knowledge_status`, `knowledge_search`,
  `knowledge_graph` (`frontend/src/hooks/useKnowledge.ts`). Canvas = `_slices.drawable`: nodes sorted by degree,
  first 1,200; edges from the first 36,000 edge rows that join two shown nodes, ≤6,000; communities that appear;
  cached in `slice.json` until next index (`_slices.py:34-61,137-203`). The Atlas **Graph** tab (`GraphTab.tsx`)
  is a different graph (Atlas facts, `atlas/graph.py`) reusing the same `CodeGraph.tsx` renderer; its code and test
  boxes come from `built_as`/`tested_by` entities created from `graph.db`.
- **Atlas Ask** — `AtlasService._citations` → `KnowledgeService.citations` = `crg search` on both estates
  (12 hits) → round-robin 10 across estates → read code excerpts (`atlas/service.py:612-622`,
  `knowledge/service.py:277-298`, `knowledge/evidence.py:36-93`). Failure ⇒ an `index_failed` omission.
- **Atlas fact ↔ code (spec 126 A16)** — `atlas/loop.py:85` → `code_job.sync_estates` (per estate, when
  `graph.db` mtime changed since last seen in this process, `code_job.py:58-67`) → `code_sync.sync`: file hashes
  vs last `code` source version; changed/gone files ⇒ re-resolve each citation, re-judge, or retire with
  `code_gone`; then queue `code_cite` for every live claim. `code_cite.cite`: `candidates()` (Atlas's own
  word-overlap search over Function/Class bodies, then uncovered top-level ranges) → rule check → LLM judge
  (`CODE_BRIEF`, cached per claim revision/place/file hash) → `code_symbol`/`code_range` evidence with **no
  code text stored** (`code_cite.py:1-18,311-345`). Reverse lookup `GET …/atlas/code/facts`
  (`code_cite.facts_citing`, `api/routers/atlas.py:660-674`).
- **Build/test links** — `build_links.link_built` (graph nodes overlapping Build's diff hunks → `task built_as
  symbol`), `tests_run` (JUnit test case → test node by name → `TESTED_BY`/`CALLS` → `symbol tested_by test`);
  the tests job waits for the workspace re-index (`tests_run.py:407-410`).
- **Blast radius** — `GET …/atlas/blast-radius` → `lineage.blast_radius`: BFS over `CALLS` callers, then the facts
  naming those code entities (`lineage.py:289-379`).
- **Discover** — inventory fallback (`discovery/units.py`, wired in `discovery/interpretive.py:132-135` from
  `worker/phases/archeology.py:247-249`); structure (`discovery/code_map.structure_of` → `graph_units.coupling`)
  feeds `api/document_artifacts.py:72,83-99`, `worker/phases/discover_compose.py:89`, the Scope page edges
  (`api/routers/scope.py:74-84`) and the executive-summary prompt "HOW THE PARTS CONNECT (parsed from the code,
  not inferred)" (`discovery/synthesis.py:134-161`); "where to look" docs = `search_seam()` → `pointers()`
  (`discovery/document_retrieval.py:121-157`, `document_where_to_look.py:96-109,125-160`), also mounted into the
  build workspace (`worker/phases/build.py:249`).
- **Behaviour ledger** — `worker/phases/knowledge.py:107` passes the service as `SymbolIndex`; destination
  (`workspace`) symbols matched by lower-cased leaf name / route path segment; four states
  matched/absent/not_indexed/unbuilt (`core/bel/target.py:66-178`). Atlas's ledger reader turns ledger rows into
  `code_symbol` citations via `code_graph.resolve` (`atlas/readers/ledger.py:118-151`, called from
  `atlas/extract_job.py:186`).

---

## 5. Per-file table (all on `origin/feature/atlas`)

| Path | Lines | Responsibility |
|---|---:|---|
| `src/synthetix/knowledge/__init__.py` | 9 | Package doc: port + service public, adapter private (ADR-0043). |
| `src/synthetix/knowledge/views.py` | 312 | The `KnowledgeGraph` port (index/stats/search/graph/symbols/files/neighbours) and its value objects. |
| `src/synthetix/knowledge/paths.py` | 71 | Where graphs live, which tree each estate is, `estate_root` file, `strip_root`. |
| `src/synthetix/knowledge/service.py` | 386 | Policy: estates, `RADIOACTIVE` excludes, search/citations/ask orchestration, factories, staleness word. |
| `src/synthetix/knowledge/evidence.py` | 127 | Round-robin hits across estates, attach code excerpts, literal mention scan. |
| `src/synthetix/knowledge/excerpts.py` | 281 | Bounded file reading: symbol spans, file headers, term scan, shared char budget. |
| `src/synthetix/knowledge/answerer.py` | 133 | LLM prose over citations — no production caller on this branch. |
| `src/synthetix/knowledge/adapters/__init__.py` | 5 | Declares vendor knowledge lives only here. |
| `src/synthetix/knowledge/adapters/crg.py` | 270 | `CrgKnowledgeGraph`: build+embed, stats/symbols/files/neighbours via SQL, search via CLI. |
| `src/synthetix/knowledge/adapters/_process.py` | 151 | Runs the CLI: binary lookup, env pinning, barred flags, registry guard, ignore file, timeouts. |
| `src/synthetix/knowledge/adapters/_store.py` | 118 | Read-only SQLite access, path relativising, root recovery, indexing-marker age. |
| `src/synthetix/knowledge/adapters/_slices.py` | 203 | Builds and caches the capped, degree-ranked, community-coloured canvas. |
| `src/synthetix/worker/indexing.py` | 113 | Worker claims an index request and records succeeded/failed with the tool's message. |
| `src/synthetix/runs/index_queue.py` | 56 | Records an index request row (`run.index_jobs`). |
| `src/synthetix/db/dao/index_jobs.py` | 186 | SQL for index requests: request, claim with lease, finish. |
| `src/synthetix/api/routers/knowledge.py` | 241 | HTTP: status, index (202), search, graph canvas, neighbours. |
| `src/synthetix/api/knowledge_view.py` | 101 | Maps port dataclasses onto wire schemas. |
| `src/synthetix/discovery/graph_units.py` | 325 | Symbols → Discover inventory envelope; canvas → file-level coupling + clusters. |
| `src/synthetix/discovery/units.py` | 186 | "Language walk first, code map only if the walk found nothing" wrapper for Discover. |
| `src/synthetix/discovery/code_map.py` | 47 | Reads the source estate's coupling live for documents; never raises. |
| `src/synthetix/discovery/interpretive.py` | 181 | Wires `walk_then_map` into the Discover pass. |
| `src/synthetix/discovery/document_retrieval.py` | 157 | Turns a search into resolvable pointers, with matched/dropped/failure counts. |
| `src/synthetix/discovery/document_where_to_look.py` | 235 | Security / edge-case / workflow documents as pointer lists from fixed keyword queries. |
| `src/synthetix/discovery/document_sources.py` | 290 | Document provenance incl. `instrument` (COBOL walk vs tree-sitter map). |
| `src/synthetix/discovery/synthesis.py` | 309 | Puts parsed edges/clusters into the executive-summary LLM prompt. |
| `src/synthetix/worker/phases/archeology.py` | 471 | Discover phase; passes the knowledge service as the code-map reader. |
| `src/synthetix/worker/phases/knowledge.py` | 249 | Builds the behaviour ledger (destination name matching) and reports map stats. |
| `src/synthetix/core/bel/target.py` | 178 | Resolves ledger rows to destination symbols by name (lead, never verification). |
| `src/synthetix/api/routers/scope.py` | 199 | Scope page; shows code-map call edges. |
| `src/synthetix/api/document_artifacts.py` | 99 | Feeds code-map structure into document builds. |
| `src/synthetix/worker/phases/discover_compose.py` | 171 | Same, at the end of a Discover pass. |
| `src/synthetix/atlas/code_graph.py` | 260 | Atlas's own `CodeGraph` over `graph.db`: Function/Class nodes, file hashes, word-overlap search, ranges, resolve. |
| `src/synthetix/atlas/code_sync.py` | 191 | After a graph moves: diff file hashes, re-resolve/retire citations, queue `code_cite`. |
| `src/synthetix/atlas/code_cite.py` | 432 | Fact → candidate code places → rule check + LLM judge → checked citations; reverse lookup. |
| `src/synthetix/atlas/code_job.py` | 103 | Atlas loop steps: sync estates whose `graph.db` mtime moved; run `code_cite` jobs with keys bound. |
| `src/synthetix/atlas/build_links.py` | 327 | Build diff hunks → overlapping graph nodes → `built_as` links; requests workspace re-index. |
| `src/synthetix/atlas/tests_run.py` | 654 | Runs tests, maps JUnit cases to graph test nodes → `TESTED_BY`/`CALLS` → `tested_by` links. |
| `src/synthetix/atlas/lineage.py` | 386 | Lineage walk; blast radius = `CALLS` callers BFS mapped back to facts. |
| `src/synthetix/atlas/readers/ledger.py` | 189 | Links behaviour-ledger rules to code symbols via `code_graph.resolve`. |
| `src/synthetix/atlas/service.py` | 1161 | Atlas facade; Ask pulls code citations from `KnowledgeService.citations`. |
| `src/synthetix/atlas/store.py` | 405 | Atlas SQLite file in `.knowledge/`; `open_read_only` used for `graph.db`. |
| `src/synthetix/atlas/checks.py` | 301 | Holds `check_code_symbol` (no caller in `src/`). |
| `src/synthetix/atlas/loop.py` | 248 | Atlas background loop; calls `code_job.sync_estates` each tick. |
| `frontend/src/pages/knowledge/KnowledgePage.tsx` | 141 | The Code map tab: estate cards, search, canvas. |
| `frontend/src/pages/knowledge/parts/CodeGraph.tsx` | 281 | Canvas renderer (also used by Atlas Graph tab and Thing page). |
| `frontend/src/hooks/useKnowledge.ts` | 135 | React Query hooks over the knowledge routes. |
| `pyproject.toml` (C13 contract, 472-485) | — | import-linter: `views`/`service`/`answerer` may not import `subprocess` or the adapters. |
| `Dockerfile.worker` (110-134) | — | Installs the tool (CPU torch, `igraph`, readable tool dir). |
| `scripts/knowledge_graph.sh` | 82 | Dev-only indexing of this repo (`just kg-build`). |

---

## 6. Weaknesses

### 6.1 Already recorded in the findings log (summaries)

| ID | Status | What was found |
|---|---|---|
| **SYN-F-023** | NOT-A-BUG | All index jobs failed because the tool wasn't on PATH (2026-10-14); retry did not happen (attempt 1 of 3). Missing tool is a deployment fault surfaced as `last_error`. |
| **SYN-F-372** | NEEDS-DECISION | Node `id` is AUTOINCREMENT and re-minted on every re-index of a file (3→6 after a one-line edit); `qualified_name` holds the absolute path and changes on a file move. Atlas must key evidence on repo-relative path + name + file hash. **Adopted** by `atlas/code_graph.py` (docstring lines 8-12), but not by `build_links`/`tests_run`/`lineage` (they key on `qualified_name`). |
| **SYN-F-373** | OPEN | On Bridge CRM (Express + React, 1,030 symbols / 246 files / 9,688 edges, 14 s): (1) `.md` documents are never indexed (no parser); (2) facts in route registrations, `app.js`, config, `i18n.init`, `axios.create`, store files are module-level calls, not symbols, so search never lands on them. Citation baseline: backend 84/100 right file (Access control 42%), frontend 80/98 (UI/config 30%); combined with docs 91/200 vs 190/200 with the right files. |
| **SYN-F-374** | OPEN | (About V3, the legacy reference) V3's destination AKM reads only `.cs/.go/.ts/.tsx`; JS repos give 1 entity, 0 edges. Context for why the rebuild moved to a tree-sitter tool. |
| **SYN-F-379** | OPEN | On voice-assessment-agent (Python): `module.func()` calls are stored with a bare target name (`start_assessment`), so only 322/2,094 non-test CALLS edges resolve to a node (589/3,461 overall). Matters for anything walking edges (blast radius, `tests_run` CALLS fallback, coupling). |
| **SYN-F-442** | OPEN | Bridge CRM spike: server 89 files / 228 functions / 2,477 CALLS (456 resolve); client 157 files / 549 functions / 5,213 CALLS (1,651 resolve); builds < 1 s. Of 94 gold code citations, 69 fall inside a node, 17 in an indexed file but outside any node (route tables, constants, config), 8 in files not indexed (package.json, README, CLAUDE.md). Confirmed SYN-F-372 live. Verdict: good enough to link facts to code if keyed on path+name+hash, plus file-range evidence for top-level code — which A16's `code_range` (D17) now implements. |

Related entries worth reading alongside: **SYN-F-029** (OPEN — "where to look" documents always fail with an
empty engagement id; still present: `document_where_to_look.py:218-220` reads `inventory["engagement_id"]`,
which the real inventory never has), **SYN-F-053** (OPEN — Discover never builds the graph), **SYN-F-377/378**
(mention scan drops identifiers; budget spent on the first long file alphabetically), **SYN-F-441/443** (no
fact→old-code link; Ask does a second blind search — A16 is the answer on this branch), **SYN-F-449** (a plain
Claude Code agent with Read/Grep/Glob scored 45/50 vs the layer's 20/50 and a BM25 baseline's 24/50 on Bridge
CRM round 2), **SYN-F-552** (Python INHERITS: 0/56 resolve, bare names).

### 6.2 Seen in the code during this read

Labels: **observed** = the code plainly does this (read, not run); **suspected** = plausible consequence, not
reproduced.

1. **Absolute server paths reach the browser and an LLM prompt via the canvas** — *observed in code.*
   `_slices._compute` sets `GraphNode.id = qualified_name` and `label = name` straight from `nodes`, and edge
   `source/target` straight from `edges` (`_slices.py:159-171,186-188`); only `path` is relativised. Per our own
   `paths.py:55-57`, those strings hold the absolute root (and a `File` node's name *is* the absolute path).
   They flow to `GET …/knowledge/graph`, to `graph_units.coupling` (`from_symbol`/`to_symbol`), the Scope page
   and the executive-summary prompt. Contradicts the `Symbol` contract in `views.py:49-52`. (Logged SYN-F-553.)
2. **Canvas edges are a table-order sample** — *observed.* `select … from edges … limit 36000` with no
   `ORDER BY`, then kept only if both ends are among the 1,200 shown nodes (`_slices.py:176-189`). On the
   45,674-node / 492,679-edge legacy graph cited in the module docstring, most edges between hubs can be missed;
   `coupling()` (Discover documents) inherits the hole. *Suspected:* `CONTAINS` edges inflate `File` degree, so
   files may crowd the 1,200 slots. (SYN-F-554.)
3. **Atlas code sync can read a graph mid-build** — *observed* that `code_job._moved` keys only on `graph.db`
   mtime and never checks the `.indexing` marker (`code_job.py:58-67`); *suspected* that a partial file list makes
   `code_sync` retire live citations as `code_gone` (`code_sync.py:79-98`) until the next tick. (SYN-F-555.)
4. **Seam leak: the tool's schema is known outside the adapter** — *observed.* `atlas/code_graph.py`,
   `build_links.py`, `tests_run.py`, `lineage.py` run SQL on `nodes`/`edges`/`is_test`/`TESTED_BY`/`CALLS`
   directly, and `api/routers/knowledge.py:50` imports `KnowledgeGraphError` from `adapters.crg`. Swapping the
   tool means rewriting Atlas modules too, despite `crg.py:1` ("the only package that knows this tool exists")
   and the C13 contract (which covers only `views/service/answerer`). Two parallel abstractions
   (`KnowledgeGraph` port vs `atlas.code_graph.CodeGraph`). (SYN-F-557.)
5. **Dead paths** — *observed:* `KnowledgeService.ask()` / `answerer.py` / `scan_mentions` have no production
   caller (Atlas uses `citations()`; the knowledge router passes no answerer, `api/routers/knowledge.py:70-72`);
   `atlas/checks.check_code_symbol` has no caller in `src/`, so `built_as` code evidence is never re-checked
   against the graph. (SYN-F-556.)
6. **Atlas fact→code linking ignores the tool's search and embeddings** — *observed.* `CrgGraph.search` is a
   word-overlap count (words ≥4 letters minus a stoplist) over every Function/Class body, reading each file per
   query (`code_graph.py:229-238`); the `embed` step only serves the Code map search and Ask citations.
   *Suspected:* slow on large estates (no index; `files()` hashes every file on first call).
7. **Full rebuild every time** — *observed:* only `build` is called, never `update`; the port promises
   incremental (`views.py:229-235`). Build's own incrementality is the tool's business; our side always deletes the
   canvas cache and re-embeds.
8. **Root handling is fragile in three ways** — *observed:* `embed` is passed the unresolved root while `build` gets
   the resolved one (`crg.py:72,89,117`); `relative()` falls back to a bare filename for a path outside the root
   (`_store.py:78-83`), which `document_retrieval` then renders as an openable pointer; Atlas `CrgGraph` drops
   every node whose path does not resolve under the **recorded** root (`code_graph.py:211-215`), so a tree moved
   since indexing (spec 049 renames) yields an empty Atlas graph while `crg.search` still works.
9. **`build_links._overlapping` has no kind filter** — *observed* (`build_links.py:227-232`); *suspected* that
   `File` nodes (and test nodes) overlapping any hunk become `built_as` targets for every task.
10. **Name-only matching** — *observed:* ledger targets (`core/bel/target.py:166-177`, first same-named symbol
    wins), `tests_run.tested_symbols` (`name = nodeid.rsplit("::",1)[-1]`, so parametrised `test_x[a]` will not
    match — *suspected*), Atlas `resolve` (unique name fallback). Combined with SYN-F-379/552 bare targets, edge
    walks under-report.
11. **Code-map "language" is alphabetical, not dominant** — *observed:* `stats.languages` comes from
    `group by language` and `units()` takes `[0]` as the envelope language (`crg.py:137-139`,
    `graph_units.py:151`). *Suspected:* a mostly-Python estate with one `.go` file is labelled `go`.
12. **"Where to look" mixes estates** — *observed:* `pointers()` calls `search` without `estates`, so both
    estates are searched for a legacy-facing document, then `hits[:12]` keeps source-first order
    (`document_retrieval.py:134-138`). Masked today by SYN-F-029.
13. **`built_at` = `max(nodes.updated_at)`** — *suspected* to under-report freshness if the tool leaves unchanged
    rows' `updated_at` alone; drives the "fresh/today/stale" word (`crg.py:130-144`, `service.py:371-386`).

---

*Prepared read-only; no file in the Synthetix repo was changed. New observations above were logged as
SYN-F-553 … SYN-F-557 in `~/.claude/synthetix-findings.md` per the standing instruction.*
