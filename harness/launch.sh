#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 RUN_DIR" >&2
  exit 2
fi

run_dir=$(realpath "$1")
meta="$run_dir/run.json"
[[ -f "$meta" ]] || { echo "missing $meta" >&2; exit 2; }

run_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$meta")
session="lain-$run_id"
script_dir=$(cd "$(dirname "$0")" && pwd)
raw_dir="$run_dir/artifacts/raw"
status_dir="$run_dir/artifacts/status"
endpoint=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shared_lain"]["endpoint"])' "$meta")
port=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shared_lain"]["port"])' "$meta")
lain_config=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shared_lain"]["config"])' "$meta")
state_home=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shared_lain"]["state_home"])' "$meta")
agy_config_changed=0

restore_on_error() {
  local code=$?
  if (( code != 0 && agy_config_changed == 1 )); then
    agy_config="$HOME/.gemini/config/mcp_config.json"
    if [[ -f "$raw_dir/agy-mcp-config.before.json" ]]; then
      cp "$raw_dir/agy-mcp-config.before.json" "$agy_config"
    else
      agy mcp remove lain >/dev/null 2>&1 || true
    fi
  fi
  return "$code"
}
trap restore_on_error EXIT

tmux has-session -t "$session" 2>/dev/null && {
  echo "tmux session already exists: $session" >&2
  exit 2
}

launch_window() {
  local role=$1
  local command
  command=$("$script_dir/agent-command.py" "$run_dir" "$role")
  local gate="$status_dir/$role.start"
  local wrapped
  printf -v wrapped 'while [[ ! -f %q ]]; do sleep 0.05; done; %s; code=$?; printf "%%s\\n" "$code" > %q; exit "$code"' "$gate" "$command" "$status_dir/$role.exit"
  tmux new-window -d -t "$session" -n "$role" "bash -lc $(printf %q "$wrapped")"
  tmux pipe-pane -o -t "$session:$role" "exec stdbuf -oL tee -a $(printf %q "$raw_dir/$role.log")"
  tmux set-option -w -t "$session:$role" remain-on-exit on
  touch "$gate"
}

server="XDG_STATE_HOME=$(printf %q "$state_home") lain server --config $(printf %q "$lain_config") --transport http --port $(printf %q "$port") --log-level info"
tmux new-session -d -s "$session" -n lain-server "bash -lc $(printf %q "$server")"
tmux pipe-pane -o -t "$session:lain-server" "exec stdbuf -oL tee -a $(printf %q "$raw_dir/lain-server.log")"
tmux set-option -w -t "$session:lain-server" remain-on-exit on

ready=0
for _ in $(seq 1 120); do
  if python3 - "$port" <<'PY'
import socket, sys
try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.2):
        pass
except OSError:
    raise SystemExit(1)
PY
  then
    ready=1
    break
  fi
  sleep 0.5
done
[[ "$ready" == 1 ]] || { echo "shared LAIN server did not become ready" >&2; exit 1; }

# AGY currently stores MCP configuration globally. Preserve it byte-for-byte,
# point only the `lain` entry at this run, and let the supervisor restore it.
if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); raise SystemExit(0 if "agy" in d["agent_types"].values() else 1)' "$meta"; then
  agy_config="$HOME/.gemini/config/mcp_config.json"
  if [[ -f "$agy_config" ]]; then
    cp "$agy_config" "$raw_dir/agy-mcp-config.before.json"
  else
    touch "$raw_dir/agy-mcp-config.was-missing"
  fi
  agy mcp add lain "$endpoint"
  agy_config_changed=1
fi

observer="python3 -u $(printf %q "$script_dir/observe.py") $(printf %q "$run_dir")"
tmux new-window -d -t "$session" -n observer "bash -lc $(printf %q "$observer")"
tmux pipe-pane -o -t "$session:observer" "exec stdbuf -oL tee -a $(printf %q "$raw_dir/observer.log")"
tmux set-option -w -t "$session:observer" remain-on-exit on

# Start the contender only after the raw LAIN log proves that the holder owns the
# canary. This makes contention deterministic without faking either agent call.
launch_window holder
"$script_dir/wait-for-claim.py" "$run_dir" holder lain-contention-canary.txt
launch_window contender

# Avoid Git-index races between the contention phase and the substantive Tauri
# phase. The holder and contender still overlap with each other.
for _ in $(seq 1 360); do
  [[ -f "$status_dir/holder.exit" && -f "$status_dir/contender.exit" ]] && break
  sleep 1
done
[[ -f "$status_dir/holder.exit" && -f "$status_dir/contender.exit" ]] || {
  echo "contention agents did not finish within 360 seconds" >&2
  exit 1
}

launch_window tauri
launch_window plugins

supervisor="$(printf %q "$script_dir/supervise.sh") $(printf %q "$run_dir") $(printf %q "$session")"
tmux new-window -d -t "$session" -n supervisor "bash -lc $(printf %q "$supervisor")"
tmux pipe-pane -o -t "$session:supervisor" "exec stdbuf -oL tee -a $(printf %q "$raw_dir/supervisor.log")"
tmux set-option -w -t "$session:supervisor" remain-on-exit on
agy_config_changed=0

echo "run directory: $run_dir"
echo "tmux session:  $session"
echo "attach with:   tmux attach -t $session"
