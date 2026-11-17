# B — How `code-review-graph` 2.3.8 works inside

How the tool parses code, what it extracts and how it stores it. Researched 2026-11-17 by reading the installed source and running the tool on a tiny sample.

- **Installed at:** `~/.local/share/uv/tools/code-review-graph/lib/python3.12/site-packages/code_review_graph/` (uv tool, Python 3.12). Below, `parser.py:N` means a line in that package.
- **Main files:**
  - `parser.py` holds all extraction (16,355 lines).
  - `graph.py` holds the SQLite store.
  - `incremental.py` holds the build/update pipeline and the ignore file.
  - `postprocessing.py` and the `*_resolver.py` modules resolve references across files.
  - `migrations.py` and `embeddings.py` hold the derived tables.
- **How the labels were checked:**
  - **observed** means seen in the code and confirmed by running it on the sample. The sample is in the scratchpad at `…/scratchpad/crg-sample/`: Python, TS/TSX and Java files with imports, calls and subclasses.
  - Per-language snippets were also run through `CodeParser.parse_bytes` (`…/scratchpad/probe-scripts/probe.py`).
  - **suspected** means inferred from the code only.
  - Nothing outside the scratchpad was parsed. The tool was not reinstalled or modified.

---

## 0. The pipeline at a glance

1. **Find files.** `collect_all_files` (`incremental.py:1027`) uses `git ls-files` and falls back to `rglob` when the folder is not a git repo. It then:
   - drops ignored paths (`DEFAULT_IGNORE_PATTERNS` plus `.code-review-graphignore`);
   - drops symlinks, binaries and overlong paths;
   - drops any file whose `detect_language()` returns `None`.
2. **Parse each file into nodes and edges.** `CodeParser.parse_bytes` (`parser.py:2600`):
   - picks the language from the extension, with a shebang fallback for files that have no extension;
   - sends special formats to their own parsers: Vue, Svelte, notebooks, Blade, VB.NET, ReScript, SQL, Ansible, Spring config;
   - otherwise parses with tree-sitter;
   - pre-scans the file scope to build `import_map` (local name → module) and `defined_names`;
   - walks the tree recursively (`_extract_from_tree`, `parser.py:6248`);
   - resolves typed receivers;
   - resolves bare call targets against definitions in the same file (`_resolve_call_targets`, `parser.py:4652`);
   - mirrors TESTED_BY edges from test→production CALLS edges.
   - Files are parsed in parallel with a process pool. Files under 8, or `CRG_SERIAL_PARSE=1`, are parsed serially (`incremental.py:1255-1330`).
3. **Store.** `GraphStore.store_file_nodes_edges` (`graph.py:359`) deletes the file's old rows and upserts the new ones in one transaction.
4. **Resolve across the repo.** These run after the parse and never fail the build (`incremental.py:1338-1344`):
   - the Python import resolver (`python_resolver.py`);
   - ReScript, Spring DI, Spring events, Temporal and HCL resolvers;
   - the scoped `Class::method` resolver (`scoped_resolver.py`, which covers PHP, Rust and C#).
5. **Post-processing** (`postprocessing.py:31`):
   - resolve bare CALLS targets and TESTED_BY sources when there is evidence;
   - resolve C++ scoped calls;
   - compute signatures;
   - rebuild the FTS5 index;
   - trace flows;
   - detect Leiden communities with igraph;
   - optionally embed.

---

## 1. Supported languages

### 1.1 Extension → language map (exact, `EXTENSION_TO_LANGUAGE`, `parser.py:715-788`)

| Extension(s) | Language label → grammar |
|---|---|
| `.py` | python |
| `.js` `.jsx` `.mjs` | javascript. **JSX is parsed by the JS grammar.** `.cjs` is **not** mapped. |
| `.ts` | typescript |
| `.tsx` | tsx |
| `.astro` | typescript. The whole `.astro` file goes to the TS grammar (suspected: frontmatter only parses). |
| `.go` | go |
| `.rs` | rust |
| `.java` | java |
| `.cs` | csharp |
| `.rb` | ruby |
| `.c` `.h` `.xs` | c. A `.h` file is promoted to cpp when the C++ grammar finds C++-only syntax (`parser.py:2615-2627`, `_CPP_HEADER_EVIDENCE_TYPES` `:791`). |
| `.cpp` `.cc` `.cxx` `.hpp` `.hh` | cpp. Qt macros are masked before parsing. |
| `.kt` | kotlin. **`.kts` is not mapped**, although the Kotlin import resolver looks for `.kts` files (`parser.py` ~`:13773`). |
| `.swift` | swift |
| `.php` | php. `*.blade.php` is detected as `blade` and handled by `_parse_blade` (`:2938`). |
| `.scala` | scala |
| `.sol` | solidity |
| `.vue` | vue → `_parse_vue` (`:2972`) sends the `<script>` blocks to JS/TS |
| `.svelte` | svelte → `_parse_svelte` (`:3083`) |
| `.dart` | dart |
| `.r` / `.R` | r |
| `.pl` `.pm` `.t` | perl |
| `.lua` / `.luau` | lua / luau |
| `.m` | objc. **`.mm` is not mapped.** |
| `.sh` `.bash` `.zsh` `.ksh` | bash. Files without an extension also land here via the shebang (`SHEBANG_INTERPRETER_TO_LANGUAGE`, `:815`). |
| `.ex` `.exs` | elixir |
| `.ipynb` | notebook → `_parse_notebook` (`:3213`). The cells are parsed as Python, R and so on. Databricks `.py` exports are detected by their header (`:2650`). |
| `.zig` | zig |
| `.ps1` `.psm1` `.psd1` | powershell |
| `.jl` | julia |
| `.res` `.resi` | rescript → **regex** parser `_parse_rescript` (`:3957`). No grammar. |
| `.gd` | gdscript |
| `.nix` | nix |
| `.sv` `.svh` `.v` `.vh` | verilog |
| `.vb` | vbnet → **regex** parser `_parse_vbnet` (`:3534`). There is no VB grammar (`get_parser('vb')` → LookupError). |
| `.sql` | sql → `_parse_sql` (`:4353`): tree-sitter plus a regex fallback for `CREATE PROCEDURE` |
| `.tf` `.hcl` | hcl |
| `.yml` `.yaml` | yaml. Generic YAML is **skipped** (`:2700`). Only Ansible paths (`ansible`) and Spring `application*.yml` (`spring_config`) are parsed. |
| `.properties` | only Spring config files are parsed. Any other `.properties` file returns `None` (`:2506-2509`). |

**Not mapped at all** (observed: `detect_language` returns `None`, so the file is never collected):

- COBOL (`.cbl`, `.cob`, `.cpy`);
- VB6 (`.bas`, `.cls`, `.frm`);
- `.kts`, `.mm`, `.cjs`;
- Groovy, Haskell, OCaml, F#, Fortran;
- `.html`, `.json`, `.toml`, `.md`.

**Adding a language yourself.** You can add more through `.code-review-graph/languages.toml` (`custom_languages.py:1-80`, at most 20 entries). Each entry names a grammar from the language pack plus node-type lists for functions, classes, imports and calls. Built-in names and extensions always win. Name, import and call extraction for these languages is generic, based on field names (`parser.py:15707`, `:15983`).

### 1.2 Which grammars ship

Grammars come from the `tree_sitter_language_pack` 0.13.0 wheel: 170 precompiled `*.abi3.so` files in `tree_sitter_language_pack/bindings/` (351 MB). The separate packages `tree_sitter_c_sharp`, `tree_sitter_yaml` and `tree_sitter_embedded_template` are also installed. Every grammar is probed in a subprocess before first use, with a 5 s timeout, `CRG_PARSER_LOAD_TIMEOUT_SECONDS` (`parser.py:460-575`).

`get_parser()` was tested for each label:

- **Load:** python, javascript, typescript, tsx, go, rust, java, csharp, ruby, cpp, c, kotlin, swift, php, scala, solidity, vue, dart, r, perl, lua, luau, objc, bash, elixir, zig, powershell, svelte, julia, gdscript, nix, verilog, sql, hcl, yaml.
- **Also load, but no extension maps to them:** **cobol**, fortran, groovy, haskell, ocaml, markdown, html, json, toml. These are reachable only through `languages.toml`.
- **Fail:** `vb`, `embedded_template`. VB.NET therefore uses the regex fallback, and VB6 has nothing.

### 1.3 Full extraction vs recognised only

| Level of extraction | Languages |
|---|---|
| **Full**: classes, functions, imports, calls and inheritance, with specific handling | Python, JS/JSX, TS/TSX, Java, C#, Go, Rust, C++, Kotlin, PHP, Scala, Swift, Dart, Solidity, Julia |
| **Good but one part missing** | **Ruby**: no inheritance branch in `_get_bases`. **ObjC**: no inheritance; imports are raw text. **C**: structs only, includes not resolved. **Perl**: imports are raw text, no bases. **Lua/Luau**: no classes, handled through table-OOP code. **R**: classes found by matching call patterns. |
| **Structural / special** | Bash (functions, `source` imports, every command is a CALLS). SQL (tables and views as Class, functions/procedures, table refs as IMPORTS_FROM, **no calls**, `_CALL_TYPES['sql']=[]` `:1194`). HCL/Terraform. Nix. Elixir. Zig. PowerShell (no imports). Verilog. GDScript. |
| **Regex fallbacks** | VB.NET (`.vb`), ReScript |
| **Recognised but not extracted** | generic YAML, non-Spring `.properties`. Grammars that ship without a mapping, such as COBOL, are not even recognised. |

Notes on languages the brief singled out:

- **JSX/TSX:**
  - `.jsx` is parsed by the JS grammar and `.tsx` by the tsx grammar.
  - A capitalised JSX element, such as `<Badge/>`, becomes a **CALLS** edge to the component (`_extract_jsx_component_call`, `:11915`; seen in the sample: `App → Badge`).
  - Arrow and const components are Function nodes (`_extract_js_var_functions`, `:8625`).
- **TS types:**
  - `interface`, `type` aliases and `enum` are stored as **`Class`** nodes, not `Type` (`_CLASS_TYPES`, `:906-913`).
  - Type-position identifiers emit REFERENCES edges (`_extract_ts_type_reference`, `:12006`).
  - Method signatures inside an interface are **not** nodes (observed).
- **Vue/Svelte:** only the `<script>` blocks are parsed. The template is ignored (observed: `.vue` gives the `go` function and its import and call).
- **Kotlin, Go, Java, C#, PHP, Ruby, Rust, Swift, Dart:** see §6 and the table at the end.
- **COBOL, VB6:** not supported, as above.
- **SQL:** see the table above.
- **Shell:** see the table above.

---

## 2. What it extracts

### 2.1 Node kinds (`NodeInfo`, `parser.py:670`)

- `File`, `Class`, `Function`, `Test`, `Type`.
- Framework kinds: `Endpoint` (Spring/Laravel routes), `Scheduler` (Spring `@Scheduled`), `ConfigProperty` (Spring config keys).
- **There is no `Method` kind.** A method is a `Function` with `parent_name` set to its class.
- A function becomes **`Test`** (`_extract_functions`, `:10410-10418`) when one of these is true:
  - its name matches `_TEST_PATTERNS` (`test_*`, `Test*`, `*_test`, …, `:1215`);
  - it carries a test annotation (`@Test`, `#[test]`, `[Fact]`-style attributes, PHPUnit `#[Test]`/`@test`, `:1252`).
- In a JS/TS test file, calls to `describe`/`it`/`test`/`suite`/`before*`/`after*` become **synthetic Test nodes** named `it:desc@L12` (`:10676-10732`).
- `Type` is emitted only for ReScript (`:4149`) and Julia (`:7636`).
- Interfaces, traits, protocols, enums, structs, records, Go `type` declarations, modules (Ruby/Julia), Solidity contracts and SQL tables/views are **all `Class`**. Swift keeps the actual keyword in `extra.swift_kind` (`:10174-10189`).

### 2.2 Edge kinds (`EdgeInfo`, `parser.py:694`). Counts are of `kind="…"` occurrences in `parser.py`.

| Edge | Meaning and source |
|---|---|
| `CONTAINS` | File → Class/Function, Class → method, enclosing function → synthetic Test (`:10261`, `:10560`) |
| `CALLS` | caller (function, or File for module-level code) → callee (`_extract_calls`, `:10640`). JSX component use and Solidity modifier invocations also count. |
| `IMPORTS_FROM` | File → resolved file path, or the raw module string (`_extract_imports`, `:10610`) |
| `INHERITS` | class → base. **One edge kind for both "extends" and "implements"** in Java, TS, C#, PHP, Kotlin, Swift, Dart and Scala (`:10270-10280`). |
| `IMPLEMENTS` | **only** Rust `impl Trait for T` (`_extract_rust_impl`, `:12592`) and VB.NET `Implements` (`:3830`) |
| `TESTED_BY` | production → test. Mirrored from every CALLS edge whose source is a Test node (`:2771-2786`). |
| `REFERENCES` | function used as a value (map, array or argument, `:12075`); TS type references; Python base-class references; Julia qualified definitions |
| `DEPENDS_ON`, `DEPENDS_ON_CONFIG`, `INJECTS`, `HANDLES`, `TRIGGERS`, `PUBLISHES`, `CONSUMES`, `PRODUCES`, `TEMPORAL_STUB` | Framework edges: Spring DI and config, Spring events, Kafka, Temporal, Laravel, dbt, Ansible (`:8919-10030`, `:11355+`) |

### 2.3 How each relationship is detected

The four tree-sitter node-type tables are in `parser.py`:

- `_CLASS_TYPES` at `:900`
- `_FUNCTION_TYPES` at `:998`
- `_IMPORT_TYPES` at `:1073`
- `_CALL_TYPES` at `:1129`

| Language | Class nodes | Function nodes | Import nodes | Call nodes |
|---|---|---|---|---|
| python | `class_definition` | `function_definition` | `import_statement`, `import_from_statement` | `call` |
| js | `class_declaration`, `class` | `function_declaration`, `method_definition`, `arrow_function` (plus const/field/member-assigned functions) | `import_statement` (plus literal `require()`/`import()`, `:13339`) | `call_expression`, `new_expression` |
| ts/tsx | as js plus `interface_declaration`, `type_alias_declaration`, `enum_declaration` | as js | as js | as js |
| go | `type_declaration` | `function_declaration`, `method_declaration` (attached to the receiver type) | `import_declaration` | `call_expression` |
| rust | `struct_item`, `enum_item`, `trait_item` (`impl_item` is a scope) | `function_item`, `function_signature_item` | `use_declaration` | `call_expression`, `macro_invocation` |
| java | `class_declaration`, `interface_declaration`, `enum_declaration` | `method_declaration`, `constructor_declaration` | `import_declaration` | `method_invocation`, `object_creation_expression`, `method_reference` |
| c# | class/interface/enum/struct/record(+struct) `_declaration` | `method_declaration`, `constructor_declaration` | `using_directive` | `invocation_expression`, `object_creation_expression` |
| c | `struct_specifier`, `type_definition` | `function_definition` | `preproc_include` | `call_expression` |
| cpp | `class_specifier`, `struct_specifier` | `function_definition`, `declaration`, `field_declaration` | `preproc_include` | `call_expression` |
| ruby | `class`, `module` | `method`, `singleton_method` | `call` (`require`/`require_relative`) | `call`, `method_call` |
| kotlin | `class_declaration`, `object_declaration` | `function_declaration` | `import_header` | `call_expression` |
| swift | class/struct/protocol `_declaration` | function/init/deinit/subscript `_declaration` | `import_declaration` | `call_expression` |
| php | class/interface/trait/enum `_declaration` | `function_definition`, `method_declaration` | `namespace_use_declaration` | function/member/scoped/nullsafe call expressions, `object_creation_expression` |
| scala | class/trait/object/enum `_definition` | `function_definition`, `function_declaration` | `import_declaration` | `call_expression`, `instance_expression`, `generic_function` |
| dart | `class_definition`, `mixin_declaration`, `enum_declaration` | `function_signature` | `import_or_export` | special sibling scan (`_extract_dart_calls_from_children`, `:7254`) |
| objc | class_interface/implementation, category_interface, protocol_declaration | `method_definition`, `function_definition` | `preproc_include` | `message_expression`, `call_expression` |
| bash | none | `function_definition` | `source`/`.` commands (`:7218`) | `command` |
| sql | handled by `_parse_sql` | handled by `_parse_sql` | handled by `_parse_sql` | none |

**Base-class extraction** is `_get_bases` (`parser.py:15282-15486`):

- python: `argument_list` identifiers and attributes.
- java: `superclass` / `super_interfaces > type_list`.
- c#: `base_list`.
- kotlin: the raw text of `delegation_specifier`, `type_identifier` and similar children.
- scala: `extends_clause`.
- cpp: `base_class_clause`.
- ts/js: `class_heritage` > extends/implements clauses.
- solidity: `inheritance_specifier`.
- go: embedded fields (intended).
- dart: `superclass`/`mixins`/`interfaces`.
- swift: `inheritance_specifier`.
- julia: `<:`.
- php: `base_clause`/`class_interface_clause`.
- **No branch** for ruby, objc, perl, lua, c or rust structs. Rust uses impl-trait IMPLEMENTS instead.

**Import target text** comes from `_extract_import` (`parser.py:15488-15770`). Swift, Perl, ObjC and others fall through to "record the whole statement text" (`:15766-15768`).

**Call name** comes from `_get_call_name` (`parser.py:15772-15981`). It takes the rightmost identifier of a member expression (`obj.a.b()` → `b`), the full text of a scoped identifier (`Vec::new`), and `Class::method` for a PHP scoped call.

---

## 3. How references are resolved

### 3.1 Calls

1. **At the call site** (`_extract_calls`, `:10640`):
   - The **caller** is `file::Class.func`, or the File path for module-level code (`:10735-10747`).
   - **Receiver handling** applies to python, kotlin, java, js, ts, tsx, php, csharp, cpp, go and rust (`_TYPED_CALL_LANGUAGES`, `:5048`). The receiver is stored in `extra.receiver`.
   - A `self`/`cls`/`this` receiver resolves to `file::EnclosingClass.method` (`:10829-10834`).
   - **Any other receiver keeps the bare method name** (`target = call_name`, `:10841`). Later typed-receiver and post-pass resolvers may fix it.
   - With no receiver, `_resolve_call_target` (`:14213`) applies:
     - a name defined in the file → `file::name`;
     - a name in `import_map` → resolve the module to a file, chase JS/TS re-exports and barrels (`_resolve_exported_symbol`, `:14273`), then `resolved_file::name`;
     - otherwise **the bare name is kept** (e.g. `printf`, `join`, `helper`).
   - Calls inside statically dead branches are skipped (`if False`/`TYPE_CHECKING` in Python, `if (0)`/`if false` in JS/TS/Go, `#if 0` in C/C++; `:220-440`).
2. **Typed receivers** (`_collect_typed_call_targets` `:5060`, `_apply_typed_call_targets` `:5515`):
   - Parameter, field and local-variable type annotations or constructions map `var.method()` to `Type.method`.
   - This covers Java/Kotlin/C#/PHP/Python/TS, e.g. `new Base().run()` in the sample.
3. **Same-file pass** (`_resolve_call_targets`, `:4652`):
   - Builds a bare-name → qualified-name table from the file's own nodes.
   - Rewrites bare targets, taking the **first** definition with that name.
   - C++ keeps overloads explicit (`ambiguous_targets`/`unresolved_targets` in `extra`).
   - Go method receivers are matched against the receiver type.
4. **Post-build bare-endpoint pass** (`GraphStore._resolve_bare_endpoints`, `graph.py:965`):
   - A bare CALLS target (or TESTED_BY source) is rewritten **only when exactly one** Function, Test or Class with that name lives either in the call-site file or in a file the call-site file IMPORTS_FROM.
   - C# namespaces are mapped to declaring files through `File.extra.csharp_namespaces` (`graph.py:1041-1065`).
   - The original bare name is kept in `extra.bare_call_target`.
   - A globally unique name is deliberately **not** enough (docstring `graph.py:733-744`).
   - Several candidates → stays bare, with `ambiguous_targets` in extra.
5. **Specialised post-passes:**
   - `scoped_resolver.py`: PHP `Class::method`, Rust `Type::fn`/`Self::`, and C# receiver-scoped calls. Rewritten edges get `confidence_tier='INFERRED'` (`scoped_resolver.py:480`).
   - Spring DI, Spring event, Temporal, HCL and ReScript resolvers.
   - `jedi_resolver.py` exists, but **nothing calls `enrich_jedi_calls`** and `jedi` is not installed. It is dead in 2.3.8 (observed by grep).

### 3.2 Unresolved targets

Unresolved targets stay as **bare strings** in `edges.target_qualified`, with **no node created**. Examples from the sample: `join`, `of`, `Runnable`, `java.util.List`. Joins to `nodes.qualified_name` simply miss. Every edge still gets `confidence=1.0, confidence_tier='EXTRACTED'` unless a resolver sets `INFERRED`, so the tier does not separate resolved edges from unresolved ones. Check `target_qualified NOT LIKE '%::%'` or a failed join instead.

### 3.3 INHERITS / IMPLEMENTS targets

These are **never resolved to qualified names**. They stay the bare base text (`Animal`, `Base`, `Shape`; observed). The bare-endpoint pass covers only CALLS and TESTED_BY. As a result `query inheritors_of Animal` works by name (observed), and same-named classes in different files are indistinguishable (suspected). Python additionally emits a **REFERENCES** edge File → resolved base class (`pkg/dog.py → pkg/base.py::Animal`, observed).

### 3.4 Imports (`_resolve_module_to_file` `:13570` / `_do_resolve_module` `:13596`)

The module is resolved to an **absolute file path** only for these languages:

| Language | Resolution |
|---|---|
| Python | walk up directories looking for `a/b.py` or `a/b/__init__.py`, plus a repo-wide unique-suffix pass in `python_resolver.py` |
| JS/TS/Vue | relative paths with extension and `index` probing, plus tsconfig `paths` (`tsconfig_resolver.py`) |
| Dart | relative and `package:` via pubspec |
| Rust | Cargo crate layout |
| Java | `a/b/C.java`, walking up |
| Kotlin | `.kt`/`.kts`, walking up |
| PHP | composer PSR-4 |
| Bash, Nix, Zig | relative paths |

**Not resolved** (the raw string is kept): Go, C#, C/C++ includes, Ruby, Swift, Scala, Perl, ObjC, Elixir, Julia, Verilog, SQL. Wildcard imports in Java and Kotlin also stay raw.

### 3.5 Aliasing (`_collect_import_names`, `:13394`)

| Language | Alias handling |
|---|---|
| JS/TS | `import {a as b}` records the original name (`_JS_IMPORT_ORIGINAL_PREFIX_KEY`), so calls to `b` resolve to `file::a`. Default and namespace imports are mapped. `require` destructuring is handled (`:13366`). |
| Java/Kotlin | `import x.Y` and Kotlin `as` are handled. `static` and `*` imports are skipped in the map. |
| Rust | `use` and `as` are handled. |
| Julia | `as` and selected imports are handled. |
| PHP | `use … as` is handled. |
| Python | `from m import f as g` maps **`g` → m**, then qualifies with the *alias*. Observed: `h()` produced `CALLS → pkg/base.py::h`, a dangling target that should have been `helper`. `import os.path as osp` emits **no IMPORTS_FROM edge** (aliased_import has no `dotted_name` child; observed). |

---

## 4. The graph.db SQLite schema

The database lives at `<repo>/.code-review-graph/graph.db`, with a `.gitignore` written next to it. `--data-dir` moves it elsewhere, and a legacy `<repo>/.code-review-graph.db` is migrated (`cli.py:1700-1712`). Schema version 9 after migrations (`migrations.py`).

```sql
nodes(id INTEGER PK AUTOINCREMENT, kind TEXT, name TEXT,
      qualified_name TEXT UNIQUE, file_path TEXT, line_start, line_end,
      language, parent_name, params, return_type, modifiers,
      is_test INTEGER, file_hash TEXT, extra TEXT JSON, updated_at REAL,
      signature TEXT, community_id INTEGER)            -- graph.py:75 + migrations
edges(id INTEGER PK AUTOINCREMENT, kind, source_qualified, target_qualified,
      file_path, line, extra TEXT JSON, confidence REAL DEFAULT 1.0,
      confidence_tier TEXT DEFAULT 'EXTRACTED', updated_at)   -- graph.py:94
metadata(key PK, value)  -- schema_version, last_updated, last_build_type,
                         -- cpp_identity_version, last_postprocessed_at, postprocess_level
flows, flow_memberships, communities, community_summaries, flow_snapshots,
risk_index                                            -- migrations.py:84-200
nodes_fts  (FTS5 over name, qualified_name, file_path, signature; porter)  -- migrations.py:151
embeddings(qualified_name PK, vector BLOB float32, text_hash, provider)  -- embeddings.py:1040, created on demand
```

**Identity:**

- `qualified_name` comes from `GraphStore._make_qualified` (`graph.py:2179`) and `CodeParser._qualify` (`parser.py:14378`).
- A File node uses the **absolute POSIX file path**.
- Other nodes use `"<abs_path>::<name>"` or `"<abs_path>::<Parent>.<name>"`. C++ adds namespace/class scopes joined with `.`, and overloads use an identity name.
- JS test nodes use `name@L<line>`.
- Because paths are absolute, **a graph is tied to its checkout location** (observed).

**Ids:**

- `id` is a plain autoincrement row id.
- Edges reference nodes **by qualified-name string, not by id**. There are no foreign keys, so dangling targets are allowed.
- A node upsert is keyed on `qualified_name`. Two definitions with the same qualified name in one file collapse into one row. Observed: ObjC `@interface Dog` plus `@implementation Dog` gives one node.
- An edge is deduplicated on `(kind, source, target, file_path, line)` (`graph.py:265-295`).
- Every node and edge row carries `file_path`, so re-parsing a file deletes and re-inserts only that file's rows.

**Sample counts:** 8 files → 28 nodes and 53 edges. Seen in the sample:

- CONTAINS, CALLS, IMPORTS_FROM, INHERITS, REFERENCES and TESTED_BY edges;
- `extra` keys `receiver`, `bare_call_target`, `receiver_resolution`, `import_resolution`.

---

## 5. CLI commands and flags

Read from `code-review-graph --help` and from `cli.py`.

| Command | What it does |
|---|---|
| `build [--repo R] [-q] [--skip-flows] [--skip-postprocess] [--data-dir D] [--embedding-provider P --embedding-model M]` | Full re-parse of every collected file (`full_build`, `incremental.py:1255`), removal of stale files, then the resolvers and post-processing. `--skip-flows` keeps only signatures and FTS. `--skip-postprocess` stores the raw parse only. The embedding flags refresh an *existing* embedding index explicitly; this is off by default. |
| `update [--base REF] [--brief] [--verify] [...same flags]` | Incremental update. It re-parses only the files changed since `--base`, which defaults to the commit the graph was last built at (`incremental_update`, `incremental.py:1364`). `--brief` also prints the risk and token-savings summary. `--verify` calibrates the estimate with tiktoken. |
| `postprocess [--no-flows] [--no-communities] [--no-fts]` | Re-runs the post-processing on an existing DB |
| `embed [--provider local\|openai\|google\|minimax\|voyage] [--model M]` | Vector embeddings for semantic search. The default is local `sentence-transformers` `all-MiniLM-L6-v2`; sentence_transformers and torch **are installed** here. The embedded text is name, `Parent.name`, split words, kind, directory and language (`embeddings.py:1094`), **not the source body**. Cloud providers send this source-derived text off-machine. |
| `watch`, `daemon {start,stop,status,add,remove}` | File watching and auto-update (watchdog), for one repo or many |
| `status [--json]` | Graph statistics |
| `forget PATH… [--dry-run]` | Drops files from the graph. Referrers are re-parsed as if those files were absent. |
| `query {callers_of,callees_of,imports_of,importers_of,children_of,tests_for,inheritors_of,file_summary} target` | Relationship queries |
| `impact`, `detect-changes`, `dead-code`, `search`, `flows`/`flow`, `communities`/`community`, `architecture`, `large-functions`, `refactor` | Analyses over the graph |
| `visualize`, `wiki` | HTML graph view and a markdown wiki built from communities |
| `install`/`init`, `uninstall`, `serve`/`mcp [--http]`, `register`/`unregister`/`repos`, `enrich`, `eval` | MCP and agent integration, multi-repo registry, hook enrichment, benchmarks |

**Ignore file:**

- `<repo>/.code-review-graphignore` uses gitignore-like globs. A trailing `/` means a directory at any depth; a leading `/` anchors the pattern to the root.
- A `!path` line does **not** negate explicit patterns. It only spares a directory from the automatic nested build-output exclusion (`incremental.py:472-506`, `:612-660`).
- `DEFAULT_IGNORE_PATTERNS` (`incremental.py:157+`) already excludes `node_modules`, `.venv`, `vendor`, `/dist`, `/build`, `/target`, `/bin`, `/obj`, `*.min.js`, `*.jar` and others.
- Set `CRG_NESTED_OUTPUT_SCAN=0` to turn off the nested build-output scan.

**Other environment variables:** `CRG_SERIAL_PARSE`, `CRG_PARSE_EXECUTOR`, `CRG_RECURSE_SUBMODULES`, `CRG_PARSER_LOAD_TIMEOUT_SECONDS`.

---

## 6. Known limits, per language

| Language | Limit | Status |
|---|---|---|
| Python | `from m import f as g; g()` → CALLS to a nonexistent `m.py::g` | observed |
| Python | `from .base import helper` → IMPORTS_FROM target `helper`, marked unresolved. The relative module is lost because `_extract_import` takes the first `dotted_name`, which is the imported name (`:15493-15498`). | observed |
| Python | `import x.y as z` emits no IMPORTS_FROM at all | observed |
| Python | Subscripted or generic bases (`Generic[T]`, `mod.Base[int]`) give no INHERITS. Only `identifier`/`attribute` arguments count (`:15285-15290`). Keyword `metaclass=` is ignored, which is correct. | observed |
| Python | Bare `obj.method()` on an untyped receiver stays bare (`join`). The Jedi enrichment that would fix this is not wired. | observed |
| JS/TS | `new Foo()` produces **no CALLS edge**. `_get_call_name` sees the `new` keyword as the first child (`:15775-15981`). Java `new` *is* captured. | observed |
| JS/TS | `super.draw()` resolves to the **class's own** `draw` (a self-loop) | observed |
| JS/TS | `console.log(m)` inside a function called `log` resolved to `log` itself. The bare-name pass ignores the `console` receiver. | observed |
| JS/TS | Interfaces and type aliases are stored as `Class`. Interface method signatures are not nodes. Extends and implements are both INHERITS. | observed |
| JS | `.cjs` is not mapped | observed |
| Java | `implements` is stored as INHERITS. Generic bases keep their type arguments in the target text (suspected from `:15297-15305`). INHERITS targets are bare. `java.util.*` imports are not resolved. | observed (bare); suspected (generics) |
| Kotlin | INHERITS includes **the class's own name** plus raw text such as `Animal(n)`. The branch takes the text of `type_identifier`/`delegation_specifier` children (`:15335-15344`). | observed |
| Kotlin | `.kts` is not parsed | observed |
| Go | Embedded structs give **no** INHERITS (the nesting under `field_declaration` is missed, `:15402-15413`). Imports are never resolved to files. Calls through package aliases stay bare with a receiver (`fmt.Println` → `Println`). | observed |
| C# | `new StringBuilder()` produced no CALLS edge | observed |
| C# | `using` imports stay namespace strings, bridged only through `csharp_namespaces` evidence | observed |
| C/C++ | `#include` is never resolved to a file. C has no inheritance. Overloads stay ambiguous on purpose. | observed (code) |
| Ruby | **No inheritance at all** (`class Dog < Animal` gives nothing). `include Walk` becomes a CALLS from the File. Implicit-self calls without parentheses are not captured (comment at `:15876`). | observed |
| Rust | Trait impls produce IMPLEMENTS, resolved locally. `Vec::new`-style paths stay bare unless the scoped resolver finds a unique match. Struct and enum "inheritance" does not apply. | observed |
| Swift | The import target is the whole statement text (`import Foundation`). Superclass and protocol conformance are both INHERITS. | observed |
| PHP | Good coverage (FQN imports, PSR-4, scoped calls, Laravel). `$this->run()` stays bare with a receiver unless resolved later. | observed |
| Dart | Calls inside methods are attributed to the **File**, not the method. Observed: `CALLS a.dart -> helper`. `_extract_dart_calls_from_children` runs before the function scope is known. | observed |
| ObjC | No INHERITS for `@interface Dog : Animal`. The import is raw text. Interface and implementation collapse into one node. `.mm` is not mapped. | observed |
| Scala | `with` traits are INHERITS. Import selectors are expanded. | observed |
| Bash | Every command, including `echo` and `ls`, is a CALLS edge to a bare name | observed |
| SQL | Tables and views become Class. FROM/JOIN refs become IMPORTS_FROM edges to **bare table names**, including a self-reference for `CREATE TABLE t`. Procedure calls produce no CALLS. | observed |
| COBOL | The grammar ships, but no extension is mapped, so files are skipped. Supporting it would need a `languages.toml` entry with your own node types. | observed |
| VB6 | `.bas`, `.cls` and `.frm` are skipped. `.vb` is VB.NET only, through regex (classes, Sub/Function, Inherits, Implements, Imports, calls). | observed |
| All | Unresolved targets are bare strings with no node and `confidence_tier` stays `EXTRACTED`. INHERITS targets are never resolved to qualified names. Absolute paths sit in every id. Parsing stops at an AST depth of 180 (`_MAX_AST_DEPTH`, `:5554`). | observed |

---

## Per-language summary

The columns cover parsing and extraction only; resolution quality is in §3 and §6. "Grammar ships?" means the grammar loads from the installed language pack.

| Language | Extensions | Grammar ships? | Classes | Functions | Imports | Calls | Inheritance |
|---|---|---|---|---|---|---|---|
| Python | .py, .ipynb, shebang | yes | yes | yes (methods = Function + parent) | partial: `from x import` only; `import a as b` dropped; relative imports broken | yes; untyped receivers stay bare; aliased calls mis-qualified | partial: plain names only, not `Base[T]` |
| JavaScript / JSX | .js .jsx .mjs | yes | yes | yes (arrow/const/member/field fns) | yes (ESM, literal `require`/`import()`, re-exports) | partial: `new X()` missed; JSX components = CALLS | yes (bare targets) |
| TypeScript / TSX | .ts .tsx (.astro) | yes | yes (+interface/type/enum as Class) | yes | yes (+tsconfig paths) | partial: `new` missed; `super.x()` self-loop | yes: extends + implements both INHERITS |
| Go | .go | yes | yes (all `type` decls) | yes (methods attached to receiver) | partial: raw module strings, never resolved | yes; pkg/receiver calls stay bare | no: embedding not detected |
| Rust | .rs | yes | yes (struct/enum/trait) | yes | yes (Cargo-aware) | yes; `Type::fn` via scoped resolver | partial: `impl Trait for T` → IMPLEMENTS only |
| Java | .java | yes | yes | yes (+ctors) | yes (path walk; wildcards not) | yes (typed receivers, `new`, method refs) | yes: extends + implements both INHERITS (bare) |
| C# | .cs | yes | yes (+struct/record) | yes | partial: namespace strings, bridged by namespace evidence | partial: `new T()` missed | yes (base_list, bare) |
| Kotlin | .kt (not .kts) | yes | yes | yes | yes | yes | partial: noisy (own name, `Base(args)` text) |
| Swift | .swift | yes | yes (class/struct/enum/actor/extension/protocol) | yes (+init/deinit/subscript) | partial: raw statement text | yes | yes (superclass + protocols as INHERITS) |
| PHP | .php (.blade.php) | yes | yes (+trait/enum) | yes | yes (FQN, PSR-4) | yes (+scoped resolver, Laravel) | yes |
| Ruby | .rb | yes | yes (class/module) | yes | yes (`require`, raw) | partial: no-paren implicit calls missed | **no** |
| C | .c .h .xs | yes | partial: structs/typedefs | yes | partial: header names, unresolved | yes | no (n/a) |
| C++ | .cpp .cc .cxx .hpp .hh (.h promoted) | yes | yes | yes (scoped identity, overloads) | partial: unresolved includes | yes (overload-aware) | yes (base_class_clause) |
| Scala | .scala | yes | yes | yes | yes (selectors, wildcards) | yes | yes |
| Dart | .dart | yes | yes (+mixin/enum) | yes | yes (relative, `package:`) | partial: attributed to File, not method | yes (extends/with/implements) |
| Objective-C | .m (not .mm) | yes | yes (dup collapse) | yes | partial: raw text | yes | **no** |
| Vue / Svelte | .vue .svelte | yes | from `<script>` | from `<script>` | from `<script>` | from `<script>` | from `<script>` |
| Bash/sh/zsh/ksh | .sh .bash .zsh .ksh, shebang | yes | no | yes | yes (`source`/`.`) | yes, noisy (every command) | n/a |
| SQL | .sql | yes | tables/views as Class | CREATE FUNCTION/PROCEDURE | table refs as IMPORTS_FROM (bare) | **no** | n/a |
| VB.NET | .vb | **no** (regex) | yes | yes | yes | yes | yes (Inherits → INHERITS, Implements → IMPLEMENTS) |
| VB6 | .bas .cls .frm | no | — | — | — | — | not recognised |
| COBOL | .cbl .cob .cpy | **yes, but unmapped** | — | — | — | — | not recognised (needs languages.toml) |
| Others (Perl, Lua/Luau, R, Elixir, Zig, PowerShell, Julia, GDScript, Nix, Verilog, Solidity, HCL, ReScript) | see §1.1 | yes (ReScript: no, regex) | varies (§2.3) | yes | mostly yes (PowerShell: no) | mostly yes (Nix: imports only) | Julia/Solidity yes; Perl/Lua/R/Elixir no |
