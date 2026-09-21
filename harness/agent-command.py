#!/usr/bin/env python3
"""Print a shell-safe real-agent command for one run role."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path


def q(value: str | Path) -> str:
    return shlex.quote(str(value))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("role", choices=("tauri", "plugins", "holder", "contender"))
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    meta = json.loads((run_dir / "run.json").read_text())
    repo_key = "plugins" if args.role == "plugins" else "tauri"
    repo = Path(meta["repositories"][repo_key]["path"])
    prompt = run_dir / "prompts" / f"{args.role}.md"
    agent_type = meta["agent_types"][args.role]
    model = meta["models"].get(agent_type, "")
    endpoint = meta["shared_lain"]["endpoint"]

    if agent_type == "codex":
        parts = [
            "codex", "--dangerously-bypass-approvals-and-sandbox", "exec", "--json", "--cd", str(repo),
            "-c", f"mcp_servers.lain.url={json.dumps(endpoint)}",
        ]
        if model:
            parts.extend(["--model", model])
        command = " ".join(q(part) for part in parts) + " - < " + q(prompt)
    elif agent_type == "claude":
        mcp = json.dumps({"mcpServers": {"lain": {"type": "http", "url": endpoint}}})
        parts = [
            "claude", "--print", "--verbose", "--output-format", "stream-json",
            "--include-hook-events", "--dangerously-skip-permissions",
            "--strict-mcp-config", "--mcp-config", mcp,
        ]
        if model:
            parts.extend(["--model", model])
        command = "cd " + q(repo) + " && " + " ".join(q(part) for part in parts) + " < " + q(prompt)
    elif agent_type == "agy":
        parts = [
            "agy", "--output-format", "stream-json",
            "--dangerously-skip-permissions", "--print-timeout", "600s",
            "--log-file", str(run_dir / "artifacts" / "raw" / f"{args.role}-agy.internal.log"),
        ]
        if model:
            parts.extend(["--model", model])
        parts.append("--print=" + prompt.read_text())
        # launch.sh temporarily points agy's `lain` entry at this run's shared
        # HTTP endpoint and restores the user's prior configuration afterward.
        command = "cd " + q(repo) + " && " + " ".join(q(part) for part in parts)
    else:
        raise SystemExit(f"unsupported agent type: {agent_type}")
    print(command)


if __name__ == "__main__":
    main()
