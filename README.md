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
- authenticated agent CLIs: `codex`, `claude`, `agy` built in; any other (`kimi`, `minimax`, …)
  via an `AGENT_CMD_<NAME>` template in the config (see `config.example.env`)
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

## Team-scenario battery (S1–S7)

The baseline above is one scenario. The battery adds realistic team situations:
agents joining late, handing work over, dying while holding a file, working in
parallel, spanning two repositories, and surviving a LAIN server crash. Every
scenario uses real agent processes on real Tauri code; nothing is simulated.

```bash
cp harness/config.example.env harness/config.env     # set sources, TEAM_AGENTS, AGENT_CMD_*
./harness/run-battery.sh harness/config.env          # all of S1..S7, then the report
./harness/run-battery.sh harness/config.env S3 S7    # a subset
./harness/run-scenario.py S3 --config harness/config.env --keep-session
```

| ID | Situation | What must hold |
|----|-----------|----------------|
| S1 | A edits `html.rs`; B joins later wanting the same file | B finds A via the roster, avoids the file, still ships |
| S2 | A does a third of a job, leaves a handoff note, releases; B joins | B reads the note, claims after A's release, builds on A's commit |
| S3 | H claims `platform.rs` (TTL 45 s) and is `SIGKILL`ed; R needs it | R is blocked, LAIN revokes the dead claim, R then gets it |
| S4 | Three agents, three modules of one crate, started together | No false conflicts, no overlap, three commits |
| S5 | X in tauri, Y in plugins-workspace, Z read-only architect | Claims stay in their repos, Z uses federation tools and edits nothing |
| S6 | 8 real `lain mcp` stdio processes race one claim (no LLM) | Exactly one grant — regression probe for the known state-lock fail-open |
| S7 | Shared LAIN server is `SIGKILL`ed mid-edit and restarted | WAL held data, no panic, a different agent finds the new symbol |

### Architecture

```
run-battery.sh ──► run-scenario.py <Sx> ──► report.py ──► reports/battery.md
                        │
   scenarios.py  (data: roles + timeline + check fn per scenario)
   evidence.py   (Ctx + reusable checks, categories lain / agent / code)
   lainlog.py    (parse + replay LAIN's events.jsonl)
   agents.py     (codex / claude / agy adapters + AGENT_CMD_* templates)
   prepare.py    (disposable clones, shared repos.yaml, canary, run.json)
                        │
        tmux: ONE shared `lain server` (HTTP) + one window per agent process
```

Each verdict (`runs/<id>/artifacts/scenario-verdict.json`) separates three
categories so a failure is attributable:

- `lain` — the event log shows LAIN did (or failed to do) its job;
- `agent` — the agent obeyed (or ignored) the protocol (claim before edit, commit, …);
- `code` — the **harness** reran the verification command and it exited zero.

Agent prose is never evidence. The `agent` checks that grep a transcript only
look for tool names (e.g. `leave_handoff_note`), and a scenario passes only if
the LAIN log and Git history agree.

### Findings (LAIN 0.9.0, first validation run)

Validated end to end with real `claude` processes on shallow Tauri clones. The
codex / agy / kimi / minimax adapters are untested until you run them.

| # | Finding | Evidence | Status |
|---|---------|----------|--------|
| 1 | Default tool profile (`core`) hides `register_agent`/`claim_files`; headless agents cannot recover via `load_package` (no tool-list refresh) | first S3 attempt: H gave up | worked around: server runs `LAIN_TOOL_PROFILE=session,social,notes,federation` |
| 2 | `release_files` intermittently returns `{"released":[]}` for a live claim (both relative and absolute paths); no `ClaimReleased`, claim lingers until `ttl_expired` | S1-A, S2-A, S7-W; worked for X, Y, B, R. Failing cases coincide with *inferred* claims on `*.tmp.*` files after the edit | open — S1/S2 stay red on it |
| 3 | `leave_handoff_note` is refused in a multi-repo workspace ("requires a single registered repo") | S5-Z; S2 passes once the server serves only tauri | open — S5 stays red on it |
| 4 | LAIN keeps one `events.jsonl` per server process and discards it on restart | S7; the harness archives it before each deliberate kill | harness handles it |
| 5 | After a restart the server revokes the dead session's claims; the WAL file (`graph.bin.wal`) held 2.4 MB at the crash and a different agent found W's new symbol | S7 PASS | good |
| 6 | S6 (stdio state-lock probe) did not reproduce with 4 agents x 1 iteration | smoke run only | rerun at the default 8 x 3 |

Other notes: LAIN writes one federation-wide event log (not one per repository);
all runs share one `CARGO_TARGET_DIR` (`runs/.cargo-target`) so clones do not each
build their own `target/`.
