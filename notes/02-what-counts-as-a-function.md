# Walkthrough 2 — what code-review-graph counts as a function (and a class)

Read from the installed package, version 2.3.8:
`/Users/thiru/.local/share/uv/tools/code-review-graph/lib/python3.12/site-packages/code_review_graph/`
(`parser.py` and `graph.py`). Starts where Walkthrough 1 stopped: a file's text has been handed to
tree-sitter and a syntax tree has come back. This is what sheet 2's **"Functions found"** counts.

## The path from syntax tree to a function row

| # | Step | Where | What happens |
|---|---|---|---|
| 1 | Tables loaded | `parser.py:2448-2452` | Four tables, keyed by language, say which tree-node types are a class, a function, an import, a call. |
| 2 | File row | `parser.py:2720-2729` | One `File` node per file. |
| 3 | Walk | `parser.py:6248-6530` | Goes through the tree node by node. For each node, special handlers for some languages run first, then: class? → function? → import? → call? Anything else: go one level deeper. |
| 4 | Name | `parser.py:10328-10330` (`_get_name`, `:14491`) | The function's name is read from the node. **No name → not recorded**, the walk carries on inside it. |
| 5 | Kind | `parser.py:10391-10400` | `Test` if it looks like a test (name or decorator or file), else `Function`. Both are functions for our count. |
| 6 | Identity | `parser.py:10435`, `_qualify` `:14378-14389` | `<absolute file path>::<Class>.<name>`, or `<file>::<name>` outside a class. **Parameters are not part of it, and neither is an enclosing function.** |
| 7 | Inside the body | `parser.py:10597-10607` | Walks into the function body, so nested functions and calls are found. |
| 8 | Save | `graph.py:79`, `:231-263` | `qualified_name` is `UNIQUE`; saving uses `ON CONFLICT(qualified_name) DO UPDATE`. **A second function with the same identity overwrites the first.** |

## What counts as a function, per language

From `_FUNCTION_TYPES` (`parser.py:998-1071`) and `_CLASS_TYPES` (`parser.py:900-982`). The three
languages in our test apps in full; the rest in short.

### Python
- **Function:** `function_definition` (`:999`) — covers `def` and `async def`, methods, nested functions.
- **Class:** `class_definition` (`:901`).
- **Not a function:** `lambda`.

### JavaScript and JSX (`.js`, `.jsx`, `.mjs` all use the `javascript` grammar, `:717-741`)
- **Function by node type:** `function_declaration`, `method_definition`, `arrow_function` (`:1000`).
  An `arrow_function` has no name of its own, so on its own it is dropped at step 4.
- **Function by special handler** (these give arrows and function expressions a name):
  - `const foo = () => {}` / `let bar = function () {}` — `_extract_js_var_functions` (`:8625-8694`).
    The value must be an `arrow_function`, `function_expression` or `function` (`:8621-8623`).
  - `handler = () => {}` as a class field — `_extract_js_field_function` (`:6436-6446`).
  - `app.handle = function () {}`, `exports.render = function () {}` — `_extract_js_member_functions` (`:6448-6458`, `:8795`).
- **Class:** `class_declaration`, `class` (`:902`).
- **Not a function (no name, no handler):** a callback passed as an argument —
  `router.get('/x', (req, res) => {...})`, `.map(x => ...)`, `useEffect(() => {...})`. Also a
  function wrapped in a call — `const f = useCallback(() => {...})`, `memo(...)`, `forwardRef(...)`:
  the value is a call, not one of the three types at `:8621-8623`. (Suspected from the code, to be
  measured.)

### Java
- **Function:** `method_declaration`, `constructor_declaration` (`:1005`). Interface methods without
  a body are `method_declaration` too, so they count.
- **Class:** `class_declaration`, `interface_declaration`, `enum_declaration` (`:920`).
- **Not in the lists:** `record_declaration` (Java 16+ records) and `compact_constructor_declaration`
  (a record's short constructor), `annotation_type_declaration`. A record is not a class row; its
  methods are still found by step 3's "go deeper", but filed under the outer class or the file.
- **Not a function:** lambdas.

### Other languages, short
Go: functions and methods (method attached to its receiver type, `:10335-10338`). Rust: `fn` items
and trait signatures. C#: methods and constructors — **not** properties, local functions or
operators. Kotlin: `function_declaration` only — **not** secondary constructors. Ruby, PHP, Swift,
Scala, Dart, C, C++: see `:1003-1066`. Elixir, Nix, Zig, HCL, SQL, Lua: empty lists, handled by
their own code (`:6276-6379`, `:2670`). R: classes found by call patterns, not node types (`:929`).

## Findings

| # | Finding | Where | Effect on "Functions found" | Measured? |
|---|---|---|---|---|
| F1 | **Same identity → one row.** Overloads (same name, different parameters) and two nested functions with the same name in one file collapse; the last one wins. | `:14378-14389`, `graph.py:79`, `:243` | Lower than the truth | **Yes** — see below |
| F2 | **No name → not a function.** Callbacks and inline arrow functions. | `:10328-10330` | Lower in JS/JSX | No |
| F3 | **Wrapped functions are missed** — `useCallback(...)`, `memo(...)`, `forwardRef(...)`. | `:8621-8623`, `:8660` | Lower in React code | No (suspected) |
| F4 | **Java records not a class**; compact record constructors not a function. | `:920`, `:1005` | Lower in modern Java | No |
| F5 | **Nested-function identity ignores the outer function.** `def outer(): def helper()` is stored as `file::helper`, the same as a top-level `helper`. | `:10435`, `_qualify` | Feeds F1 | Part of F1 |
| F6 | **Tests are a separate kind.** `Test` and `Function` both count as functions. | `:10391-10400` | Count both, or the number is wrong | — |
| F7 | **No error check.** A function inside a syntax error may be missing or half-read with no warning (from Walkthrough 1). | `parser.py:2705` | Lower, silently | No |

### F1 measured

Parsed every file of each test app with the tool's own parser and counted identities produced more
than once in the same file (`scratchpad/tools/dupes.py`, using `CodeParser.parse_bytes` and the
tool's own `_node_qualified`):

| App | Rows the parser produced | Identities produced more than once | Rows lost by the overwrite |
|---|---|---|---|
| voice-assessment-agent (Python) | 628 | 5 | 11 |
| crm-prototype (JS) | 336 | 9 | 13 |
| crm-prototype-client (JSX) | 707 | 0 | 0 |
| spring-petclinic (Java) | 379 | 4 | 5 |

- The voice agent's 11 explains a gap seen earlier: the build printed **628 nodes** but the table
  held **617**. 628 − 11 = 617.
- Java example: `Owner.java` has three `getPet(...)` overloads → **one** row
  (`Owner.getPet x 3`). `VetRepository.findAll` x 2 → one row.
- Python example: `tests/unit/test_agent_tools_retry.py` defines a nested `mock_post` 8 times → one row.
- JS example: `services/lead.service.js` has `phoneKey` 3 times (functions inside different functions) → one row.

## What the notebook must do because of this

1. **Define "function" the same way on both sides, per language, in writing**, before counting:
   - Python: every `def` and `async def` (`ast.FunctionDef` + `ast.AsyncFunctionDef`), lambdas excluded.
   - Java: every method and constructor declared in the file (from `javac`), lambdas excluded.
   - JavaScript/JSX: needs a decision — count only **named** functions, or also callbacks? The
     tool can never find unnamed ones (F2), so including them makes "found %" low by design.
2. **Compare function by function, not just totals.** Match on file + name + start line. A total
   can hide F1: 10 expected, 10 found looks perfect even when 2 are missing and 2 extras appear.
3. **Count `Function` + `Test` rows** for "Functions found" (F6).
4. **Report F1 separately** — "lost to same-name overwrite" — so it can be told apart from "not found".

## Not read (and why it doesn't change the count)

- The post-build resolvers (`incremental.py:1340-1346`): checked that none of them writes a
  `Function` or `Class` row. Six of the seven never touch `nodes`; `event_resolver.py:37`, `:76`
  only deletes and adds rows of kind `Event` (Spring events).
- Import and call extraction (`_extract_imports` `:10610`, `_extract_calls`): links, not in the
  sheet's columns.
