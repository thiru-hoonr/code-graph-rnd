# Code-graph R&D

Fills the team's `Ingestion_Coverage.xlsx` for **code-review-graph** (2.3.8, the version Synthetix
pins): which files it reads, which lines it understands, and how many functions it finds compared
with each language's own parser. Kept outside the Synthetix repo on purpose.

## Layout

| Path | What |
|---|---|
| `apps/<repo>/` | The repos under test, cloned with git (git-ignored) |
| `notebooks/<repo>.ipynb` | **One notebook per project**, e.g. `crm-prototype.ipynb` (JavaScript only), `voice-assessment-agent.ipynb` (Python only). It imports only the reference parsers for its own languages |
| `notebooks/coverage_lab.py` | The shared logic every project notebook calls. Language-neutral; definitions at the top |
| `notebooks/ref_<language>.py` | One reference parser per language ("functions expected"): `ref_python.py` (Python `ast`), `ref_javascript.py` (TypeScript compiler, via `js-ref/`). A new language = a new file here |
| `java-ref/` | The Java reference: `ListMethods.java` (javac's parser, parse only), run by `ref_java.py` with the JDK's `java` — needs a JDK installed |
| `js-ref/` | The JavaScript reference: `functions.mjs` + TypeScript 5.8.3 pinned in `package.json` (`node_modules` git-ignored) |
| `templates/Ingestion_Coverage.xlsx` | Untouched copy of the team's sheet |
| `results/<repo>/runs.csv` | **The run log**: one line per iteration, append-only — the history of every run |
| `results/<repo>/runs/iter-NNN_<date>_<time>/` | One iteration: `coverage.ipynb` (saved run, the sheet's "Evidence"), `Ingestion_Coverage_<repo>.xlsx`, `crg/graph.db` (git-ignored). Never overwritten |
| `results/<repo>/link-count-2026-11-17/` | Results of the earlier link-count experiment (different question) |
| `notes/` | Walkthroughs: `01` how files are picked, `02` what counts as a function |

Earlier link-count work, not used for the sheet, is in `old/` (git-ignored).

## Run one repo

```bash
cd notebooks && ./run.sh <repo>
```

Each run becomes the next iteration (`iter-002`, `iter-003`, …) with its own folder and a new line
in `runs.csv`. Earlier iterations are never touched.

Or interactively: open `notebooks/<repo>.ipynb`, Restart the kernel, Run All (kernel
`code-graph-rnd`), then Save As into the iteration folder the second cell prints.

## Add a project

1. Clone it into `apps/<repo>`.
2. Copy an existing project notebook to `notebooks/<repo>.ipynb`; set `APP`, and import the
   `ref_<language>` modules for its languages into `REFERENCES`.
3. A language with no `ref_<language>.py` still gets sheet 1 and files tried/parsed; its
   functions columns say "no reference parser for this language yet".

## Add a language

Write `notebooks/ref_<language>.py` with `LANGUAGE`, `LABEL` and `expected(root, files)` — the
contract is at the top of `ref_python.py`. Nothing else changes.

## Setup (once)

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python "code-review-graph==2.3.8" openpyxl pandas jupyterlab ipykernel papermill
.venv/bin/python -m ipykernel install --user --name code-graph-rnd
```
