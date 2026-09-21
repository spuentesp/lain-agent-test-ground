#!/usr/bin/env python3
"""Fail-closed verifier for a LAIN multi-agent evidence run."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


TOKEN = re.compile(r'("session_token"\s*:\s*")[^"]+("|$)')
CODE_SUFFIXES = {".rs", ".toml", ".js", ".ts", ".tsx", ".json"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


def events_from(path: Path) -> list[dict[str, Any]]:
    parsed = []
    if not path.exists():
        return parsed
    for raw in path.read_text(errors="replace").splitlines():
        payload = raw.split("\t", 1)[-1]
        try:
            parsed.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return parsed


def events_from_observer(path: Path) -> list[dict[str, Any]]:
    parsed = []
    if not path.exists():
        return parsed
    for raw in path.read_text(errors="replace").splitlines():
        if not raw.startswith("[") or "] " not in raw:
            continue
        payload = raw.split("] ", 1)[1].split("\t", 1)[-1]
        try:
            parsed.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return parsed


def status_code(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def event_name(event: dict[str, Any]) -> str:
    return next(iter(event), "")


def repo_relative(repo: Path, value: str) -> str:
    """Normalize LAIN's stdio-relative and HTTP-absolute paths alike."""
    path = Path(value)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(repo.resolve()))
        except ValueError:
            return value
    return str(path)


def commit_for_prefix(repo: Path, baseline: str, prefix: str) -> str | None:
    records = git(repo, "log", "--format=%H%x09%s", f"{baseline}..HEAD").splitlines()
    for record in records:
        commit, _, subject = record.partition("\t")
        if subject.startswith(prefix):
            return commit
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    meta = json.loads((run_dir / "run.json").read_text())
    artifacts = run_dir / "artifacts"
    share = artifacts / "share"
    share.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    shared = meta.get("shared_lain")
    shared_events: list[dict[str, Any]] | None = None
    if shared:
        paths = list(Path(shared["state_home"]).glob("lain/federation/*/.lain/events.jsonl"))
        shared_events = [event for path in paths for event in events_from(path)]
        archives = list((artifacts / "raw").glob("lain-events-*.jsonl"))
        shared_events.extend(event for path in archives for event in events_from(path))
        shared_events.extend(events_from_observer(artifacts / "raw" / "observer.log"))

    repo_events: dict[str, list[dict[str, Any]]] = {}
    for repo_key, info in meta["repositories"].items():
        repo = Path(info["path"])
        baseline = info["baseline"]
        head = git(repo, "rev-parse", "HEAD")
        changed = git(repo, "diff", "--name-only", f"{baseline}..{head}").splitlines()
        substantive = [name for name in changed if Path(name).suffix in CODE_SUFFIXES]
        check(f"{repo_key}: commit created", head != baseline, f"baseline={baseline[:12]} head={head[:12]}")
        check(f"{repo_key}: substantive files changed", bool(substantive), ", ".join(substantive) or "none")
        check(f"{repo_key}: independent verification", status_code(artifacts / "status" / f"verify-{repo_key}.exit") == 0, "see raw verification log")
        repo_events[repo_key] = shared_events if shared_events is not None else events_from(repo / ".lain" / "events.jsonl")

    for role in ("tauri", "plugins", "holder", "contender"):
        code = status_code(artifacts / "status" / f"{role}.exit")
        log = artifacts / "raw" / f"{role}.log"
        check(f"{role}: real process completed", code == 0, f"exit={code}")
        check(f"{role}: transcript captured", log.exists() and log.stat().st_size > 0, str(log))

    role_repo = {"tauri": "tauri", "plugins": "plugins", "holder": "tauri", "contender": "tauri"}
    ids: dict[str, str] = {}
    for role, expected_name in meta["agents"].items():
        joined = []
        for event in repo_events[role_repo[role]]:
            data = event.get("AgentJoined")
            if data and data.get("name") == expected_name:
                joined.append(data)
        if joined:
            ids[role] = max(
                joined,
                key=lambda data: (
                    data.get("started_at", {}).get("secs_since_epoch", 0),
                    data.get("started_at", {}).get("nanos_since_epoch", 0),
                ),
            )["id"]
        check(f"{role}: LAIN registration", bool(joined), expected_name)

    for role in ("tauri", "plugins", "holder"):
        agent_id = ids.get(role)
        all_events = repo_events[role_repo[role]]
        claims = [event for event in all_events if event.get("ClaimGranted", {}).get("agent_id") == agent_id]
        edits = [event for event in all_events if event.get("EditLanded", {}).get("event", {}).get("agent_id") == agent_id]
        check(f"{role}: claim granted", bool(claims), f"claims={len(claims)}")
        check(f"{role}: attributed edit observed", bool(edits), f"edits={len(edits)}")
        first_claim = next((index for index, event in enumerate(all_events) if event.get("ClaimGranted", {}).get("agent_id") == agent_id), None)
        first_edit = next((index for index, event in enumerate(all_events) if event.get("EditLanded", {}).get("event", {}).get("agent_id") == agent_id), None)
        ordered = first_claim is not None and first_edit is not None and first_claim < first_edit
        check(f"{role}: claim preceded attributed edit", ordered, f"claim_index={first_claim} edit_index={first_edit}")

    for role in ("tauri", "plugins"):
        repo_key = role_repo[role]
        repo = Path(meta["repositories"][repo_key]["path"])
        prefix = f"lain-eval({meta['agents'][role]}):"
        commit = commit_for_prefix(repo, meta["repositories"][repo_key]["baseline"], prefix)
        check(f"{role}: attributable commit", commit is not None, prefix)
        changed_by_agent = git(repo, "show", "--format=", "--name-only", commit).splitlines() if commit else []
        claimed_paths = {
            repo_relative(repo, event["ClaimGranted"]["path"])
            for event in repo_events[repo_key]
            if event.get("ClaimGranted", {}).get("agent_id") == ids.get(role)
        }
        unclaimed = sorted(path for path in changed_by_agent if path and path not in claimed_paths)
        claim_detail = ", ".join(unclaimed) if unclaimed else ("all claimed" if changed_by_agent else "no attributable commit/files")
        check(f"{role}: every committed file was claimed", bool(changed_by_agent) and not unclaimed, claim_detail)

    contender_id = ids.get("contender")
    conflicts = [
        event for event in repo_events["tauri"]
        if event.get("ConflictDetected", {}).get("agent_id") == contender_id
        and any(
            repo_relative(Path(meta["repositories"]["tauri"]["path"]), item.get("path", ""))
            == "lain-contention-canary.txt"
            for item in event.get("ConflictDetected", {}).get("conflicts", [])
        )
    ]
    check("contention: conflict detected", bool(conflicts), f"conflicts={len(conflicts)}")
    canary = Path(meta["repositories"]["tauri"]["path"]) / "lain-contention-canary.txt"
    expected = f"holder={meta['agents']['holder']}\nrun={meta['run_id']}\n"
    actual = canary.read_text() if canary.exists() else "<missing>"
    check("contention: losing agent did not overwrite canary", actual == expected, actual.strip())

    # Produce shareable evidence with session tokens removed. Agent transports can
    # escape JSON inside JSON, so replacing every observed token is more reliable
    # than matching only one textual representation.
    secrets = {
        data["session_token"]
        for all_events in repo_events.values()
        for event in all_events
        if (data := event.get("AgentJoined")) and data.get("session_token")
    }

    def sanitize(text: str) -> str:
        clean = TOKEN.sub(r'\1<redacted>\2', text)
        for secret in secrets:
            clean = clean.replace(secret, "<redacted>")
        return clean

    for repo_key, all_events in repo_events.items():
        clean = sanitize(json.dumps(all_events, indent=2))
        (share / f"{repo_key}-events.sanitized.json").write_text(clean + "\n")
    for log in (artifacts / "raw").glob("*.log"):
        clean = sanitize(log.read_text(errors="replace"))
        (share / log.name).write_text(clean)

    code_verification = [item for item in checks if item["name"].endswith(": independent verification")]
    coordination = [item for item in checks if item not in code_verification]
    verdict = {
        "schema_version": 1,
        "run_id": meta["run_id"],
        "coordination_passed": all(item["passed"] for item in coordination),
        "code_verification_passed": all(item["passed"] for item in code_verification),
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }
    (artifacts / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    for item in checks:
        marker = "PASS" if item["passed"] else "FAIL"
        print(f"[{marker}] {item['name']}: {item['detail']}")
    print(f"\nOVERALL: {'PASS' if verdict['passed'] else 'FAIL'}")
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, KeyError, json.JSONDecodeError) as error:
        print(f"verification failed closed: {error}", file=sys.stderr)
        raise SystemExit(2)
