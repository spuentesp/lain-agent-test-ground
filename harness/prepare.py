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


def agy_version() -> str:
    completed = subprocess.run(
        ["agy", "changelog", "--help"], text=True, capture_output=True, check=True
    )
    lines = (completed.stdout + completed.stderr).splitlines()
    return lines[0] if lines else f"installed at {shutil.which('agy')}"


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = parse_env(args.config.resolve())

    for command in ("git", "tmux", "lain", "codex", "claude", "agy"):
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
    shared_config.write_text(
        "data_dir: " + json.dumps(str(shared_data)) + "\n"
        "repos:\n"
        "  - id: tauri\n"
        "    source:\n"
        "      type: workspace_dir\n"
        "      path: " + json.dumps(str(tauri)) + "\n"
        "  - id: plugins-workspace\n"
        "    source:\n"
        "      type: workspace_dir\n"
        "      path: " + json.dumps(str(plugins)) + "\n"
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

    agents = {
        "tauri": f"tauri-{config.get('TAURI_AGENT', 'codex')}-{run_id}",
        "plugins": f"plugins-{config.get('PLUGINS_AGENT', 'claude')}-{run_id}",
        "holder": f"holder-{config.get('HOLDER_AGENT', 'codex')}-{run_id}",
        "contender": f"contender-{config.get('CONTENDER_AGENT', 'claude')}-{run_id}",
    }
    template_dir = ROOT / "harness" / "prompts"
    render(template_dir / "implementation.md", run_dir / "prompts" / "tauri.md", {
        "AGENT_NAME": agents["tauri"], "RUN_ID": run_id,
        "REPOSITORY": "tauri-apps/tauri", "TASK": config["TAURI_TASK"],
    })
    render(template_dir / "implementation.md", run_dir / "prompts" / "plugins.md", {
        "AGENT_NAME": agents["plugins"], "RUN_ID": run_id,
        "REPOSITORY": "tauri-apps/plugins-workspace", "TASK": config["PLUGINS_TASK"],
    })
    render(template_dir / "holder.md", run_dir / "prompts" / "holder.md", {
        "AGENT_NAME": agents["holder"], "RUN_ID": run_id,
    })
    render(template_dir / "contender.md", run_dir / "prompts" / "contender.md", {
        "AGENT_NAME": agents["contender"], "RUN_ID": run_id,
    })

    metadata = {
        "schema_version": 1,
        "run_id": run_id,
        "created_unix": time.time(),
        "run_dir": str(run_dir),
        "repositories": {
            "tauri": {"path": str(tauri), "baseline": run(["git", "rev-parse", "HEAD"], tauri)},
            "plugins": {"path": str(plugins), "baseline": run(["git", "rev-parse", "HEAD"], plugins)},
        },
        "agents": agents,
        "agent_types": {
            "tauri": config.get("TAURI_AGENT", "codex"),
            "plugins": config.get("PLUGINS_AGENT", "claude"),
            "holder": config.get("HOLDER_AGENT", "codex"),
            "contender": config.get("CONTENDER_AGENT", "claude"),
        },
        "models": {
            "codex": config.get("CODEX_MODEL", ""),
            "claude": config.get("CLAUDE_MODEL", ""),
            "agy": config.get("AGY_MODEL", ""),
        },
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
        },
        "versions": {
            "lain": run(["lain", "--version"]),
            "tmux": run(["tmux", "-V"]),
            "codex": run(["codex", "--version"]),
            "claude": run(["claude", "--version"]),
            "agy": agy_version(),
        },
    }
    (run_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(run_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"prepare failed: {error}", file=sys.stderr)
        raise SystemExit(2)
