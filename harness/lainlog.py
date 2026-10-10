"""Read and replay LAIN's raw event logs (events.jsonl).

Shared by scenario checks and wait helpers.  Event shape (serde externally
tagged): {"ClaimGranted": {"agent_id": ..., "path": ...}}, ...
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Event = dict[str, Any]


def parse_lines(path: Path) -> list[Event]:
    """Lines are `<seq>\\t<json>` or bare json; unparseable lines are skipped."""
    events: list[Event] = []
    if not path.exists():
        return events
    for raw in path.read_text(errors="replace").splitlines():
        try:
            events.append(json.loads(raw.split("\t", 1)[-1]))
        except json.JSONDecodeError:
            continue
    return events


def live_logs(meta: dict) -> list[Path]:
    """Per-repo ordered logs; ordering is only meaningful inside one file."""
    return sorted(Path(meta["shared_lain"]["state_home"]).glob("lain/federation/*/.lain/events.jsonl"))


def all_events(meta: dict) -> list[Event]:
    """Pre-crash archives (oldest first) then the live log.

    LAIN keeps one events.jsonl per server *process* and discards the previous one
    on restart, so run-scenario.py archives it right before every deliberate kill.
    """
    raw = Path(meta["run_dir"]) / "artifacts" / "raw"
    archived = sorted(raw.glob("lain-events-precrash-*.jsonl"))
    return [event for path in [*archived, *live_logs(meta)] for event in parse_lines(path)]


def kind(event: Event) -> str:
    return next(iter(event), "")


def agent_ids(events: list[Event], name: str) -> set[str]:
    return {
        data["id"] for event in events
        if (data := event.get("AgentJoined")) and data.get("name") == name
    }


def relative(repo: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(repo.resolve()))
        except ValueError:
            return value
    return str(path)


def exclusivity_violations(events: list[Event]) -> list[str]:
    """Replay the ordered log; report grants made while another agent held the path.

    Paths are compared as logged (absolute under HTTP), so same-named files in
    different repositories never collide.

    Holdings end on release, revoke, AgentLeft or HeartbeatExpired.
    """
    holders: dict[str, str] = {}
    violations: list[str] = []
    for index, event in enumerate(events):
        name = kind(event)
        body = event[name]
        if name == "ClaimGranted":
            path = str(body["path"])
            holder = holders.get(path)
            if holder and holder != body["agent_id"]:
                violations.append(f"#{index} {body['agent_id']} granted {path} while {holder} held it")
            holders[path] = body["agent_id"]
        elif name in ("ClaimReleased", "ClaimRevoked"):
            path = str(body["path"])
            if holders.get(path) == body["agent_id"]:
                del holders[path]
        elif name in ("AgentLeft", "HeartbeatExpired"):
            agent = body if isinstance(body, str) else body.get("agent_id")
            holders = {path: who for path, who in holders.items() if who != agent}
    return violations


def actor(event: Event, name: str | None = None) -> str | None:
    """Agent id an event is about: handles flat bodies, bare ids and EditLanded's nested audit event."""
    name = name or kind(event)
    body = event.get(name)
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return None
    if name == "EditLanded":
        return body.get("event", {}).get("agent_id")
    return body.get("agent_id")


def first_index(events: list[Event], predicate) -> int | None:
    return next((index for index, event in enumerate(events) if predicate(event)), None)
