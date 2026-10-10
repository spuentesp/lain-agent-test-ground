#!/usr/bin/env bash
# Run scenarios sequentially (one harness session at a time per user account),
# then aggregate.   ./harness/run-battery.sh harness/config.env [S1 S2 ...]
set -uo pipefail
config=${1:?usage: $0 CONFIG [SCENARIO...]}; shift
here=$(cd "$(dirname "$0")" && pwd)
scenarios=("$@"); [[ ${#scenarios[@]} -gt 0 ]] || scenarios=(S1 S2 S3 S4 S5 S6 S7)
overall=0
for id in "${scenarios[@]}"; do
  echo "=================== $id"
  "$here/run-scenario.py" "$id" --config "$config" || overall=1
done
"$here/report.py" || overall=1
exit "$overall"
