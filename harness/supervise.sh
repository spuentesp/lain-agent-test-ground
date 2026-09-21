#!/usr/bin/env bash
set -euo pipefail

run_dir=$(realpath "$1")
session=$2
status_dir="$run_dir/artifacts/status"
raw_dir="$run_dir/artifacts/raw"
agy_config="$HOME/.gemini/config/mcp_config.json"

for _ in $(seq 1 1800); do
  [[ -f "$status_dir/tauri.exit" && -f "$status_dir/plugins.exit" ]] && break
  sleep 1
done

if [[ -f "$raw_dir/agy-mcp-config.before.json" ]]; then
  cp "$raw_dir/agy-mcp-config.before.json" "$agy_config"
elif [[ -f "$raw_dir/agy-mcp-config.was-missing" ]]; then
  agy mcp remove lain >/dev/null 2>&1 || true
fi

index=0
while IFS= read -r event_log; do
  cp "$event_log" "$raw_dir/lain-events-final-$index.jsonl"
  index=$((index + 1))
done < <(find "$run_dir/lain-state" -type f -name events.jsonl -print)

# Stop only the run-local services. Dead agent panes and their pipe-pane logs
# remain in the tmux session for inspection.
tmux send-keys -t "$session:lain-server" C-c 2>/dev/null || true
tmux send-keys -t "$session:observer" C-c 2>/dev/null || true
printf '0\n' > "$status_dir/supervisor.exit"
