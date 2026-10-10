"""Evidence context and reusable checks for scenario verdicts.

Each check carries a category so a failure is attributable:
  lain   - the LAIN event log shows (or fails to show) LAIN doing its job
  agent  - the agent process obeyed (or ignored) the team protocol
  code   - the harness-rerun verification command exited zero
Nothing here trusts agent prose; transcripts are only searched for tool names.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import lainlog

CATEGORIES = ("lain", "agent", "code")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


def status_code(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


class Ctx:
    def __init__(self, meta: dict) -> None:
        self.meta = meta
        self.run_dir = Path(meta["run_dir"])
        self.raw = self.run_dir / "artifacts" / "raw"
        self.status = self.run_dir / "artifacts" / "status"
        self.checks: list[dict] = []
        # LAIN keeps one ordered federation log for every repository of the workspace.
        self.events = lainlog.all_events(meta)

    # -- recording -----------------------------------------------------------
    def add(self, category: str, name: str, passed: bool, detail: str = "") -> bool:
        assert category in CATEGORIES, category
        self.checks.append({"category": category, "name": name, "passed": bool(passed), "detail": detail})
        return bool(passed)

    # -- lookups -------------------------------------------------------------
    def repo_key(self, role: str) -> str:
        return self.meta["role_repos"][role]

    def repo_path(self, key: str) -> Path:
        return Path(self.meta["repositories"][key]["path"])

    def name(self, role: str) -> str:
        return self.meta["agents"][role]

    def agent_id(self, role: str) -> str | None:
        ids = lainlog.agent_ids(self.events, self.name(role))
        return sorted(ids)[-1] if ids else None

    def events_of(self, role: str) -> list[lainlog.Event]:
        return self.events

    def transcript(self, role: str) -> str:
        path = self.raw / f"{role}.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def used_tool(self, role: str, *tools: str) -> bool:
        text = self.transcript(role)
        return any(tool in text for tool in tools)

    def exit_code(self, role: str) -> int | None:
        return status_code(self.status / f"{role}.exit")

    def claims(self, role: str) -> list[lainlog.Event]:
        agent = self.agent_id(role)
        if agent is None:  # never registered: attribute nothing
            return []
        return [ev_ for ev_ in self.events if lainlog.actor(ev_, "ClaimGranted") == agent and "ClaimGranted" in ev_]

    def claimed_paths(self, role: str) -> set[str]:
        repo = self.repo_path(self.repo_key(role))
        return {lainlog.relative(repo, str(e["ClaimGranted"]["path"])) for e in self.claims(role)}

    def commit(self, role: str) -> str | None:
        key = self.repo_key(role)
        repo = self.repo_path(key)
        baseline = self.meta["repositories"][key]["baseline"]
        prefix = f"lain-eval({self.name(role)}):"
        for record in git(repo, "log", "--format=%H%x09%s", f"{baseline}..HEAD").splitlines():
            sha, _, subject = record.partition("\t")
            if subject.startswith(prefix):
                return sha
        return None

    def commit_files(self, sha: str | None, role: str) -> list[str]:
        if not sha:
            return []
        repo = self.repo_path(self.repo_key(role))
        return [line for line in git(repo, "show", "--format=", "--name-only", sha).splitlines() if line]

    def first_claim_index(self, role: str, path_suffix: str | None = None) -> int | None:
        agent = self.agent_id(role)
        if agent is None:
            return None
        repo = self.repo_path(self.repo_key(role))
        return lainlog.first_index(self.events_of(role), lambda e: (
            (c := e.get("ClaimGranted")) is not None and c["agent_id"] == agent
            and (path_suffix is None or lainlog.relative(repo, str(c["path"])).endswith(path_suffix))
        ))

    def first_event_index(self, role: str, name: str) -> int | None:
        agent = self.agent_id(role)
        if agent is None:
            return None
        return lainlog.first_index(self.events, lambda ev_: name in ev_ and lainlog.actor(ev_, name) == agent)


# -- reusable checks -----------------------------------------------------------

def check_registered(ctx: Ctx, role: str) -> None:
    ctx.add("lain", f"{role}: registered with LAIN", ctx.agent_id(role) is not None, ctx.name(role))


def check_worker(ctx: Ctx, role: str) -> str | None:
    """Process finished, registered, claimed before editing, committed only claimed files."""
    ctx.add("agent", f"{role}: process completed", ctx.exit_code(role) == 0, f"exit={ctx.exit_code(role)}")
    ctx.add("agent", f"{role}: transcript captured", bool(ctx.transcript(role)), f"raw/{role}.log")
    check_registered(ctx, role)
    claim = ctx.first_claim_index(role)
    ctx.add("lain", f"{role}: claim granted", claim is not None, f"claims={len(ctx.claims(role))}")
    edit = ctx.first_event_index(role, "EditLanded")
    ctx.add("lain", f"{role}: attributed edit observed", edit is not None, f"edit_index={edit}")
    ctx.add("agent", f"{role}: claim preceded first edit", claim is not None and edit is not None and claim < edit,
            f"claim={claim} edit={edit}")
    sha = ctx.commit(role)
    ctx.add("agent", f"{role}: attributable commit", sha is not None, (sha or "none")[:12])
    files = ctx.commit_files(sha, role)
    unclaimed = sorted(f for f in files if f not in ctx.claimed_paths(role))
    ctx.add("agent", f"{role}: committed only claimed files", bool(files) and not unclaimed,
            ", ".join(unclaimed) or ("all claimed: " + ", ".join(files) if files else "no files"))
    released = ctx.first_event_index(role, "ClaimReleased") is not None
    if released:
        detail = "ClaimReleased seen"
    elif ctx.used_tool(role, "release_files"):
        detail = "agent called release_files but LAIN logged no ClaimReleased (candidate LAIN defect; claim lingers until TTL)"
    else:
        detail = "agent never called release_files"
    ctx.add("lain" if ctx.used_tool(role, "release_files") else "agent", f"{role}: released its claims", released, detail)
    return sha


def check_reader(ctx: Ctx, role: str) -> None:
    """A read-only role must not claim, edit or commit."""
    ctx.add("agent", f"{role}: process completed", ctx.exit_code(role) == 0, f"exit={ctx.exit_code(role)}")
    ctx.add("agent", f"{role}: transcript captured", bool(ctx.transcript(role)), f"raw/{role}.log")
    check_registered(ctx, role)
    agent = ctx.agent_id(role)
    edits = [e for e in ctx.events if agent is not None and "EditLanded" in e and lainlog.actor(e, "EditLanded") == agent]
    ctx.add("agent", f"{role}: stayed read-only", not ctx.claims(role) and not edits and ctx.commit(role) is None,
            f"claims={len(ctx.claims(role))} edits={len(edits)}")


def check_exclusivity(ctx: Ctx) -> None:
    """Invariant for every scenario: no path is ever granted to two agents at once."""
    bad = lainlog.exclusivity_violations(ctx.events)
    ctx.add("lain", "no path was ever granted to two agents at once", not bad, "; ".join(bad) or f"{len(ctx.events)} events replayed")


def check_verification(ctx: Ctx, repos: tuple[str, ...]) -> None:
    for key in repos:
        code = status_code(ctx.status / f"verify-{key}.exit")
        ctx.add("code", f"{key}: independent verification", code == 0, f"exit={code} (raw/verify-{key}.log)")


def summarize(ctx: Ctx) -> dict:
    by = {c: [x for x in ctx.checks if x["category"] == c] for c in CATEGORIES}
    flags = {f"{c}_passed": all(x["passed"] for x in items) for c, items in by.items()}
    return {**flags, "passed": all(flags.values()), "checks": ctx.checks}
