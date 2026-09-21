#!/usr/bin/env python3
"""Wait until a named LAIN agent holds a claim on a specific path."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def read_events(path: Path) -> list[dict]:
    result = []
    if not path.exists():
        return result
    for raw in path.read_text(errors="replace").splitlines():
        try:
            result.append(json.loads(raw.split("\t", 1)[-1]))
        except json.JSONDecodeError:
            continue
    return result


def event_files(run_dir: Path, meta: dict) -> list[Path]:
    shared = meta.get("shared_lain")
    if shared:
        return list(Path(shared["state_home"]).glob("lain/federation/*/.lain/events.jsonl"))
    return [Path(value["path"]) / ".lain" / "events.jsonl" for value in meta["repositories"].values()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("role")
    parser.add_argument("path")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    meta = json.loads((args.run_dir.resolve() / "run.json").read_text())
    expected_name = meta["agents"][args.role]
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        all_events = [event for path in event_files(args.run_dir, meta) for event in read_events(path)]
        ids = {
            data["id"]
            for event in all_events
            if (data := event.get("AgentJoined")) and data.get("name") == expected_name
        }
        if any(
            (claim := event.get("ClaimGranted"))
            and claim.get("agent_id") in ids
            and (claim.get("path") == args.path or Path(claim.get("path", "")).name == Path(args.path).name)
            for event in all_events
        ):
            print(f"observed claim: {expected_name} -> {args.path}")
            return 0
        time.sleep(0.5)
    print(f"timed out waiting for claim: {expected_name} -> {args.path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
