#!/usr/bin/env python3
"""Create an isolated, reproducible LAIN agent-evaluation run."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import agents

ROOT = Path(__file__).resolve().parents[1]


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def run(command: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"required command is missing: {name}")


def tool_version(name: str, config: dict[str, str]) -> str:
    """Version string for an agent CLI; AGENT_VERSION_<NAME> overrides the probe."""
    override = config.get(f"AGENT_VERSION_{name.upper()}")
    probe = override.split() if override else ([name, "changelog", "--help"] if name == "agy" else [name, "--version"])
    try:
        completed = subprocess.run(probe, text=True, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return f"unknown (probe failed: {' '.join(probe)})"
    lines = (completed.stdout + completed.stderr).splitlines()
    return lines[0] if lines else f"installed at {shutil.which(name)}"


def clone(source: Path, destination: Path) -> None:
    if not (source / ".git").exists():
        raise RuntimeError(f"not a Git checkout: {source}")
    # --no-hardlinks also works when the source and run directory are mounted on
    # different filesystems. The copy is intentionally independent evidence.
    run(["git", "clone", "--local", "--no-hardlinks", str(source), str(destination)])
    run(["git", "config", "user.name", "LAIN Evaluation Harness"], destination)
    run(["git", "config", "user.email", "lain-eval@localhost"], destination)


def render(template: Path, destination: Path, replacements: dict[str, str]) -> None:
    text = template.read_text()
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    destination.write_text(text)


def prepare_workspace(config: dict[str, str], agent_types: dict[str, str],
                      served_repos: tuple[str, ...] = ("tauri", "plugins")) -> dict:
    """Create the disposable run: clones, shared LAIN config, canary, base metadata.

    `agent_types` maps role -> agent type and is validated against the built-in and
    config-declared adapters.  `served_repos` selects which clones the shared LAIN
    server registers: LAIN's handoff notes need a single-repo workspace, so scenarios
    that touch only tauri serve only tauri.  Returns run.json content (without prompts/agents).
    """
    custom = agents.custom_agents(config)
    used = sorted(set(agent_types.values()))
    for agent_type in used:
        agents.validate(agent_type, custom)
    for command in ("git", "tmux", "lain", *[a for a in used if a in agents.BUILTIN]):
        require_command(command)

    run_id = config.get("RUN_ID") or time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    if not all(ch.isalnum() or ch in "-_" for ch in run_id):
        raise RuntimeError("RUN_ID may contain only letters, digits, hyphens, and underscores")
    run_dir = ROOT / "runs" / run_id
    if run_dir.exists():
        raise RuntimeError(f"run already exists: {run_dir}")

    tauri_source = Path(config["TAURI_SOURCE"]).expanduser().resolve()
    plugins_source = Path(config["PLUGINS_SOURCE"]).expanduser().resolve()
    for path in (run_dir / "work", run_dir / "artifacts" / "raw", run_dir / "artifacts" / "status", run_dir / "prompts"):
        path.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir / "artifacts" / "raw", 0o700)

    tauri = run_dir / "work" / "tauri"
    plugins = run_dir / "work" / "plugins-workspace"
    clone(tauri_source, tauri)
    clone(plugins_source, plugins)

    port = int(config.get("LAIN_HTTP_PORT", "9876"))
    if not 1024 <= port <= 65535:
        raise RuntimeError("LAIN_HTTP_PORT must be between 1024 and 65535")
    shared_config = run_dir / "work" / "repos.yaml"
    shared_data = run_dir / "work" / ".lain-shared" / "data"
    entries = {"tauri": ("tauri", tauri), "plugins": ("plugins-workspace", plugins)}
    shared_config.write_text(
        "data_dir: " + json.dumps(str(shared_data)) + "\n"
        "repos:\n"
        + "".join(
            f"  - id: {entries[key][0]}\n    source:\n      type: workspace_dir\n"
            f"      path: {json.dumps(str(entries[key][1]))}\n"
            for key in served_repos
        )
    )

    # A tracked canary makes unauthorized writes objectively visible.
    canary = tauri / "lain-contention-canary.txt"
    canary.write_text(f"baseline={run_id}\n")
    run(["git", "add", canary.name], tauri)
    run(["git", "commit", "-m", "lain-eval: add contention baseline"], tauri)

    # Boot each repository once so LAIN creates its local graph/state paths.
    for repo in (tauri, plugins):
        run(["lain", "init", "--workspace", str(repo)], repo)
        run(["git", "add", "repos.yaml"], repo)
        run(["git", "commit", "-m", "lain-eval: configure local LAIN workspace"], repo)

    versions = {"lain": run(["lain", "--version"]), "tmux": run(["tmux", "-V"])}
    versions.update({agent_type: tool_version(agent_type, config) for agent_type in used})
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_unix": time.time(),
        "run_dir": str(run_dir),
        "repositories": {
            "tauri": {"path": str(tauri), "baseline": run(["git", "rev-parse", "HEAD"], tauri)},
            "plugins": {"path": str(plugins), "baseline": run(["git", "rev-parse", "HEAD"], plugins)},
        },
        "agent_types": agent_types,
        "custom_agents": custom,
        "models": {a: config.get(f"{a.upper()}_MODEL", "") for a in used},
        "verification": {
            "tauri": config["TAURI_VERIFY_CMD"],
            "plugins": config["PLUGINS_VERIFY_CMD"],
        },
        "source_heads": {
            "tauri": run(["git", "rev-parse", "HEAD"], tauri_source),
            "plugins": run(["git", "rev-parse", "HEAD"], plugins_source),
        },
        "shared_lain": {
            "config": str(shared_config),
            "state_home": str(run_dir / "lain-state"),
            "endpoint": f"http://127.0.0.1:{port}/mcp",
            "port": port,
            "served_repos": list(served_repos),
        },
        "versions": versions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = parse_env(args.config.resolve())

    agent_types = {
        "tauri": config.get("TAURI_AGENT", "codex"),
        "plugins": config.get("PLUGINS_AGENT", "claude"),
        "holder": config.get("HOLDER_AGENT", "codex"),
        "contender": config.get("CONTENDER_AGENT", "claude"),
    }
    metadata = prepare_workspace(config, agent_types)
    run_id = metadata["run_id"]
    run_dir = Path(metadata["run_dir"])
    agent_names = {role: f"{role}-{kind}-{run_id}" for role, kind in agent_types.items()}
    metadata["agents"] = agent_names

    template_dir = ROOT / "harness" / "prompts"
    render(template_dir / "implementation.md", run_dir / "prompts" / "tauri.md", {
        "AGENT_NAME": agent_names["tauri"], "RUN_ID": run_id,
        "REPOSITORY": "tauri-apps/tauri", "TASK": config["TAURI_TASK"],
    })
    render(template_dir / "implementation.md", run_dir / "prompts" / "plugins.md", {
        "AGENT_NAME": agent_names["plugins"], "RUN_ID": run_id,
        "REPOSITORY": "tauri-apps/plugins-workspace", "TASK": config["PLUGINS_TASK"],
    })
    for role in ("holder", "contender"):
        render(template_dir / f"{role}.md", run_dir / "prompts" / f"{role}.md", {
            "AGENT_NAME": agent_names[role], "RUN_ID": run_id,
        })

    (run_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(run_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"prepare failed: {error}", file=sys.stderr)
        raise SystemExit(2)
