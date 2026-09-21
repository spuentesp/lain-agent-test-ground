# LAIN Agent Test Ground

This repository is a harness for testing whether **real coding-agent processes** use
LAIN while working on disposable clones of Tauri and Tauri Plugins.

The files currently under `tests/` are legacy experiment artifacts. They are not a
test suite and are not accepted as evidence by this harness.

## What counts as evidence

A run passes only when all of these independent signals agree:

1. Codex, Claude, and/or AGY ran as separate OS processes in separate tmux windows.
2. Their complete terminal streams were captured.
3. LAIN's raw event logs contain the expected named registrations and file claims.
4. Attributed edit events occurred only after the corresponding claims.
5. Each implementation agent produced a substantive Git commit in its disposable
   repository.
6. The harness, not the agent, reran the configured verification command and got a
   zero exit status.
7. The contention trial recorded a conflict and the losing agent did not modify the
   protected canary.

LAIN remains cooperative coordination software. This harness tests whether agents
actually obey it; it does not describe an advisory claim as an operating-system
mutex.

## Requirements

- `git`, `tmux`, `python3`, and `lain`
- authenticated `codex`, `claude`, and `agy` CLIs
- local source clones of `tauri-apps/tauri` and `tauri-apps/plugins-workspace`

The harness creates disposable local clones. It does not edit the source clones.

## Run it

```bash
cp harness/config.example.env harness/config.env
# Edit source paths, task text, and verification commands in config.env.

./harness/prepare.py --config harness/config.env
./harness/launch.sh runs/<run-id>
tmux attach -t lain-<run-id>
```

The tmux session has one shared LAIN HTTP server, an observer, the Tauri agent,
the Plugins agent, two contention agents, and a cleanup supervisor. Agent roles
may use Codex, Claude, or Antigravity (`agy`). A shared server is essential:
separate per-agent stdio servers have separate in-memory claim registries and do
not prove cross-process coordination. `launch.sh` prints the exact session and
run directory.

AGY exposes MCP configuration globally rather than per invocation. The launcher
backs up that file, switches only the `lain` entry to the run-local HTTP server,
and the supervisor restores the original bytes once both implementation agents
finish. Do not run two harness sessions concurrently under the same user account.

After the implementation windows finish:

```bash
./harness/run-verification.sh runs/<run-id>
./harness/verify.py runs/<run-id>
```

The final command writes `artifacts/verdict.json` and exits nonzero unless every
required check passes. Share `artifacts/share/`, not the raw run directory: LAIN's
local event stream may contain session tokens.

## Reading a result

- `PASS` means the machine-checkable criteria above all passed.
- `FAIL` names the missing or contradictory evidence.
- `coordination_passed` isolates LAIN/agent evidence from
  `code_verification_passed`, while top-level `passed` remains fail-closed and
  requires both. This prevents a missing native build dependency from being
  misreported as a LAIN coordination failure—or vice versa.
- Agent statements such as “tests passed” are never themselves treated as proof.
- The Git patches, independent verification logs, sanitized LAIN events, and agent
  transcripts remain available for manual review.

The Codex MCP configuration is passed on the command line for each disposable
checkout. This follows the official project/local MCP configuration model while
avoiding changes to the user's global Codex configuration.

## Reproduce the stdio state-lock failure

`reproduce-stdio-lock-fail-open.py` launches independent real `lain mcp`
processes with a shared, run-local `XDG_STATE_HOME` and races them for the same
edit claim. Its default mode creates a fresh state-lock sentinel so every process
hits the two-second acquisition timeout and exercises the current fail-open path.

```bash
./harness/reproduce-stdio-lock-fail-open.py \
  --lain-bin /home/spuentesp/.lain/bin/lain \
  --agents 8 --iterations 3
```

Use `--natural` to remove the forced lock and probe the ordinary race repeatedly.
Artifacts and a token-free `report.json` are written below `runs/`. Exit status 1
means the coordination invariant was violated (more than one edit grant); 0 means
the selected run did not observe the issue; 2 means the reproducer itself failed.
