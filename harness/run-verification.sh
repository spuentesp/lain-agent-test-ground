#!/usr/bin/env bash
set -uo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 RUN_DIR" >&2
  exit 2
fi

run_dir=$(realpath "$1")
meta="$run_dir/run.json"
status_dir="$run_dir/artifacts/status"
raw_dir="$run_dir/artifacts/raw"

read_meta() {
  python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {"data": data}))' "$meta" "$1"
}

overall=0
for repo_key in tauri plugins; do
  repo=$(read_meta "data['repositories']['$repo_key']['path']")
  command=$(read_meta "data['verification']['$repo_key']")
  log="$raw_dir/verify-$repo_key.log"
  echo "[$repo_key] $command" | tee "$log"
  (cd "$repo" && bash -o pipefail -lc "$command") 2>&1 | tee -a "$log"
  code=${PIPESTATUS[0]}
  printf '%s\n' "$code" > "$status_dir/verify-$repo_key.exit"
  (( code == 0 )) || overall=1
done

exit "$overall"
