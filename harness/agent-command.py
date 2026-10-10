#!/usr/bin/env python3
"""Print a shell-safe real-agent command for one run role."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agents import build_command


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("role")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    meta = json.loads((run_dir / "run.json").read_text())
    if args.role not in meta["agent_types"]:
        raise SystemExit(f"unknown role '{args.role}'; run has: {', '.join(meta['agent_types'])}")
    repo_key = meta.get("role_repos", {}).get(args.role) or ("plugins" if args.role == "plugins" else "tauri")
    agent_type = meta["agent_types"][args.role]
    print(build_command(
        agent_type,
        role=args.role,
        repo=Path(meta["repositories"][repo_key]["path"]),
        prompt=run_dir / "prompts" / f"{args.role}.md",
        endpoint=meta["shared_lain"]["endpoint"],
        model=meta["models"].get(agent_type, ""),
        run_dir=run_dir,
        custom=meta.get("custom_agents", {}),
    ))


if __name__ == "__main__":
    main()
