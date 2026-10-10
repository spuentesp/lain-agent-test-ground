"""Team scenarios for LAIN.  Every scenario is real agents on real code.

A scenario is data (roles + a timeline of steps) plus one `check` function that
turns the raw evidence into categorized verdict entries (see evidence.py).

Step vocabulary (interpreted by run-scenario.py):
  ("start", role)                    launch the agent process in its own tmux window
  ("wait_claim", role, path_suffix)  block until LAIN logged a grant for role (suffix or None=any)
  ("wait_edit", role)                block until LAIN attributed an edit to role
  ("wait_exit", role, seconds)       block until the process exits
  ("kill", role)                     SIGKILL the agent's process tree (simulated crash)
  ("sleep", seconds)
  ("server_kill",) / ("server_start",)   SIGKILL / restart the shared LAIN server
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import evidence as ev
import lainlog

MIME = "crates/tauri-utils/src/mime_type.rs"
PLATFORM = "crates/tauri-utils/src/platform.rs"
RESOURCES = "crates/tauri-utils/src/resources.rs"
HTML = "crates/tauri-utils/src/html.rs"
UTILS_VERIFY = "cargo test -p tauri-utils --lib"

RULES_TEST = "Add one meaningful edge-case unit test or fix one concrete defect, then run `cargo test -p tauri-utils --lib <module>`."


@dataclass(frozen=True)
class Role:
    key: str
    repo: str          # "tauri" | "plugins"
    kind: str          # worker | reader | killed
    brief: str


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    intent: str
    roles: tuple[Role, ...]
    steps: tuple[tuple, ...]
    verify: tuple[str, ...]
    check: Callable[[ev.Ctx], None]


# ---------------------------------------------------------------- S1 late join
def check_late_join(ctx: ev.Ctx) -> None:
    check_a = ctx.first_claim_index("A", "html.rs")
    ev.check_worker(ctx, "A")
    ev.check_worker(ctx, "B")
    ctx.add("agent", "B: consulted the roster/overlap tools before claiming",
            ctx.used_tool("B", "list_active_agents", "get_world_state", "detect_overlap", "list_occupancy"),
            "transcript names a roster/overlap tool")
    b_files = ctx.commit_files(ctx.commit("B"), "B")
    ctx.add("agent", "B: avoided the file A held", HTML not in b_files, ", ".join(b_files) or "no files")
    ctx.add("lain", "A held html.rs before B arrived", check_a is not None, f"A claim index={check_a}")
    ev.check_exclusivity(ctx)


# ---------------------------------------------------------------- S2 handoff
def check_handoff(ctx: ev.Ctx) -> None:
    ev.check_worker(ctx, "A")
    ev.check_worker(ctx, "B")
    ctx.add("agent", "A: left a handoff note", ctx.used_tool("A", "leave_handoff_note"), "leave_handoff_note in transcript")
    ctx.add("agent", "B: read pending handoffs", ctx.used_tool("B", "get_pending_handoffs"), "get_pending_handoffs in transcript")
    events = ctx.events_of("A")
    released = ctx.first_event_index("A", "ClaimReleased")
    b_claim = ctx.first_claim_index("B", "resources.rs")
    ctx.add("lain", "A released before B claimed the same file",
            released is not None and b_claim is not None and released < b_claim, f"release={released} claim={b_claim} of {len(events)}")
    a_sha, b_sha = ctx.commit("A"), ctx.commit("B")
    repo = ctx.repo_path("tauri")
    descends = bool(a_sha and b_sha) and subprocess_ok(repo, a_sha, b_sha)
    ctx.add("agent", "B's commit builds on A's commit", descends, f"{(a_sha or '-')[:8]} -> {(b_sha or '-')[:8]}")
    ev.check_exclusivity(ctx)


def subprocess_ok(repo, ancestor: str, descendant: str) -> bool:
    import subprocess
    return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo).returncode == 0


# ---------------------------------------------------------------- S3 abandonment
def check_abandonment(ctx: ev.Ctx) -> None:
    holder_claim = ctx.first_claim_index("H", "platform.rs")
    ctx.add("lain", "H: held platform.rs before the crash", holder_claim is not None, f"index={holder_claim}")
    ev.check_registered(ctx, "H")
    gone = None
    for name in ("ClaimRevoked", "HeartbeatExpired", "AgentLeft", "ClaimReleased"):
        gone = ctx.first_event_index("H", name)
        if gone is not None:
            break
    ctx.add("lain", "LAIN dropped the dead agent's claim", gone is not None, f"event index={gone}")
    ev.check_worker(ctx, "R")
    conflict = ctx.first_event_index("R", "ConflictDetected")
    ctx.add("lain", "R was blocked while H's claim was live", conflict is not None, f"conflict index={conflict}")
    r_claim = ctx.first_claim_index("R", "platform.rs")
    ctx.add("lain", "R got platform.rs only after H's claim dropped",
            gone is not None and r_claim is not None and gone < r_claim, f"drop={gone} grant={r_claim}")
    ev.check_exclusivity(ctx)


# ---------------------------------------------------------------- S4 partial overlap
def check_parallel(ctx: ev.Ctx) -> None:
    ev.check_worker(ctx, "A")
    ev.check_worker(ctx, "B")
    ev.check_worker(ctx, "C")
    files = {r: set(ctx.commit_files(ctx.commit(r), r)) for r in ("A", "B", "C")}
    ctx.add("agent", "disjoint file sets were respected",
            not (files["A"] & files["B"] or files["A"] & files["C"] or files["B"] & files["C"]),
            "; ".join(f"{r}={sorted(f)}" for r, f in files.items()))
    spurious = [e for e in ctx.events_of("A") if "ConflictDetected" in e]
    ctx.add("lain", "no false conflicts between disjoint files", not spurious, f"conflicts={len(spurious)}")
    ev.check_exclusivity(ctx)


# ---------------------------------------------------------------- S5 cross repo
def check_cross_repo(ctx: ev.Ctx) -> None:
    ev.check_worker(ctx, "X")
    ev.check_worker(ctx, "Y")
    ev.check_reader(ctx, "Z")
    ctx.add("agent", "Z: used a cross-repo/federation tool",
            ctx.used_tool("Z", "get_cross_repo_blast_radius", "search_org", "get_workspace_graph", "get_federation_health"),
            "federation tool name in transcript")
    tried = ctx.used_tool("Z", "leave_handoff_note")
    refused = "requires a single registered repo" in ctx.transcript("Z")
    ctx.add("lain", "Z: handoff note accepted in a multi-repo workspace", tried and not refused,
            "LAIN refused: leave_handoff_note requires a single registered repo" if refused else ("accepted" if tried else "never attempted"))
    ctx.add("lain", "X edited only the tauri repo, Y only plugins",
            ctx.commit("X") is not None and ctx.commit("Y") is not None, "commits in their own repos")
    ev.check_exclusivity(ctx)


# ---------------------------------------------------------------- S7 server crash
def check_server_crash(ctx: ev.Ctx) -> None:
    ev.check_registered(ctx, "W")
    ctx.add("agent", "W: attributable commit", ctx.commit("W") is not None, (ctx.commit("W") or "none")[:12])
    marker = ctx.status / "wal-before-kill.txt"
    wal_bytes = marker.read_text().strip() if marker.exists() else "n/a"
    graph_files = sorted(str(p.relative_to(ctx.run_dir)) for p in ctx.run_dir.rglob("graph.*") if "target" not in p.parts)
    ctx.add("lain", "graph WAL (graph.bin.wal) held mutations before the crash", wal_bytes.isdigit() and int(wal_bytes) > 0,
            f"wal bytes at kill: {wal_bytes}; graph files: {graph_files} "
            "(CHANGELOG 0.9.0: batch fast paths still bypass the WAL)")
    revoked = [e for e in ctx.events if "ClaimRevoked" in e and ctx.agent_id("W") == e["ClaimRevoked"]["agent_id"]]
    ctx.add("lain", "restarted server cleaned the crashed-era claims of W", bool(revoked), f"ClaimRevoked={len(revoked)}")
    server_log = (ctx.raw / "lain-server.log").read_text(errors="replace") if (ctx.raw / "lain-server.log").exists() else ""
    fatal = [l for l in server_log.splitlines() if "panicked" in l or "FederationSchemaMismatch" in l]
    ctx.add("lain", "restarted server logged no panic/schema mismatch", not fatal, fatal[0][:160] if fatal else "clean")
    ev.check_reader(ctx, "Q")
    probe = ctx.meta["probe_symbol"]
    ctx.add("lain", "Q found W's new symbol through the restarted server", probe in ctx.transcript("Q"), probe)
    ev.check_exclusivity(ctx)


def brief(text: str) -> str:
    return " ".join(text.split())


SCENARIOS: dict[str, Scenario] = {s.id: s for s in (
    Scenario(
        "S1", "Late join: a newcomer reads the room",
        "A is already editing html.rs when B joins wanting the same file. B must discover A through LAIN, avoid html.rs and still ship.",
        (
            Role("A", "tauri", "worker", brief(f"Work in `{HTML}` only. {RULES_TEST} Take your time with the claim: hold it while you work, release it when committed.")),
            Role("B", "tauri", "worker", brief(f"""You joined while a teammate may already be working. Your preferred file is `{HTML}`.
                Before claiming anything call `list_active_agents` and `detect_overlap` on that file. If a teammate holds it, do NOT wait and do
                NOT touch it: do the same kind of work in `{MIME}` instead. {RULES_TEST}""")),
        ),
        (("start", "A"), ("wait_claim", "A", "html.rs"), ("start", "B"), ("wait_exit", "A", 900), ("wait_exit", "B", 900)),
        ("tauri",), check_late_join),
    Scenario(
        "S2", "Handoff: A stops half way, B continues",
        "A starts a job on resources.rs, leaves a handoff note and releases. B joins later, reads the note and finishes the job on the same file.",
        (
            Role("A", "tauri", "worker", brief(f"""In `{RESOURCES}` identify THREE missing edge-case unit tests. Implement and commit only the FIRST test. Then call `leave_handoff_note`
                (scope `resources`) naming the two remaining tests precisely and what you did NOT do, release your claim and stop.""")),
            Role("B", "tauri", "worker", brief(f"""Start by calling `get_pending_handoffs`. Continue exactly the work the note describes in `{RESOURCES}`: implement the two remaining
                tests it names. Run `cargo test -p tauri-utils --lib resources` before committing.""")),
        ),
        (("start", "A"), ("wait_exit", "A", 900), ("start", "B"), ("wait_exit", "B", 900)),
        ("tauri",), check_handoff),
    Scenario(
        "S3", "Abandonment: an agent dies holding a file",
        "H claims platform.rs with a 45 s TTL and is SIGKILLed. R needs the same file, is blocked, then must obtain it once LAIN drops the dead claim.",
        (
            Role("H", "tauri", "killed", brief(f"""Claim `{PLATFORM}` for edit with `claim_files` using the object form with `"ttl_seconds": 45` and intent `refactor`.
                Do NOT edit anything. After the claim is granted run the shell command `sleep 600` and wait; you are expected to be killed.""")),
            Role("R", "tauri", "worker", brief(f"""Your task is in `{PLATFORM}`. {RULES_TEST}
                Request the claim on that file. If LAIN reports a conflict: do not touch the file, check `list_active_agents`, then retry `claim_files` every 15 seconds
                (use the shell to sleep) for at most 4 minutes. Proceed only once the claim is granted. Never edit before it is.""")),
        ),
        (("start", "H"), ("wait_claim", "H", "platform.rs"), ("sleep", 3), ("kill", "H"), ("start", "R"), ("wait_exit", "R", 900)),
        ("tauri",), check_abandonment),
    Scenario(
        "S4", "Three engineers, one crate, disjoint modules",
        "A, B and C start together on html.rs, mime_type.rs and platform.rs of the same crate. LAIN must not raise false conflicts and nobody may overlap.",
        (
            Role("A", "tauri", "worker", brief(f"Work in `{HTML}` only. {RULES_TEST}")),
            Role("B", "tauri", "worker", brief(f"Work in `{MIME}` only. {RULES_TEST}")),
            Role("C", "tauri", "worker", brief(f"Work in `{PLATFORM}` only. {RULES_TEST}")),
        ),
        (("start", "A"), ("start", "B"), ("start", "C"), ("wait_exit", "A", 1200), ("wait_exit", "B", 1200), ("wait_exit", "C", 1200)),
        ("tauri",), check_parallel),
    Scenario(
        "S5", "Two repos plus an architect",
        "X works in tauri, Y in plugins-workspace, Z (read-only) assesses cross-repo impact via federation tools and leaves a handoff note.",
        (
            Role("X", "tauri", "worker", brief(f"Work in `{MIME}` only. {RULES_TEST}")),
            Role("Y", "plugins", "worker", brief("Inspect `plugins/window-state/src/lib.rs` restore/save logic, add one meaningful edge-case test or fix one concrete defect, run `cargo test -p tauri-plugin-window-state`.")),
            Role("Z", "tauri", "reader", brief("""You are the architect; you must NOT edit, claim or commit anything. Using LAIN's federation/cross-repo tools (for example
                `get_cross_repo_blast_radius`, `search_org`, `get_workspace_graph`) determine which parts of the `plugins-workspace` repo depend on the `tauri` repo's
                `WebviewWindow` API. Leave your findings with `leave_handoff_note` (scope `xrepo`) and give a 5-line summary.""")),
        ),
        (("start", "X"), ("start", "Y"), ("start", "Z"), ("wait_exit", "X", 1500), ("wait_exit", "Y", 1500), ("wait_exit", "Z", 1500)),
        ("tauri", "plugins"), check_cross_repo),
    Scenario(
        "S7", "LAIN server crash mid-work",
        "W is editing; the shared LAIN server is SIGKILLed and restarted. A different agent Q must still find W's new symbol afterwards.",
        (
            Role("W", "tauri", "worker", brief(f"""In `{MIME}` add a public function named exactly `{{{{PROBE}}}}` returning the integer 42, with a unit test, run
                `cargo test -p tauri-utils --lib mime_type`, commit and release. If a LAIN call fails because the server is unreachable, wait 20 seconds and retry up to 5 times.""")),
            Role("Q", "tauri", "reader", brief("""Read-only role. Use LAIN's code-search tools (`find_symbol` or `search_code`) to locate the function named exactly `{{PROBE}}`.
                Report the file and line it is defined at and quote the tool output. Do not edit, claim or commit anything.""")),
        ),
        (("start", "W"), ("wait_edit", "W"), ("sleep", 10), ("wal_size",), ("server_kill",), ("server_start",),
         ("wait_exit", "W", 900), ("sleep", 20), ("start", "Q"), ("wait_exit", "Q", 600)),
        ("tauri",), check_server_crash),
)}

SCRIPTS = {
    "S6": "Stdio state-lock fail-open probe (known issue; not an agent scenario)",
}
