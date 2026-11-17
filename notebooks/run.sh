#!/usr/bin/env bash
# Run one project's notebook as a new iteration, keeping the executed notebook as evidence.
# Usage (from notebooks/):  ./run.sh <repo>      e.g.  ./run.sh crm-prototype
# Each project has its own notebook here: notebooks/<repo>.ipynb.
set -euo pipefail
APP="${1:?usage: ./run.sh <repo folder under apps/>}"
cd "$(dirname "$0")"
NB="$APP.ipynb"
[ -f "$NB" ] || { echo "no notebook $NB for this project — create one first" >&2; exit 1; }
RUN="$(../.venv/bin/python -c "import coverage_lab as C; print(C.next_run_id('$APP'))")"
OUT="../results/$APP/runs/$RUN"
mkdir -p "$OUT"
echo "repo: $APP   notebook: $NB   iteration: $RUN   output: $OUT"
../.venv/bin/papermill "$NB" "$OUT/$NB" -p APP "$APP" -p RUN "$RUN"
echo "done: $OUT"
