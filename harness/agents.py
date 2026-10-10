"""Agent adapters: turn (agent type, repo, prompt) into one shell command.

Built-in adapters: codex, claude, agy.  Any other agent (kimi, minimax, ...) is
declared in the config as a command template:

    AGENT_CMD_KIMI=kimi --print --mcp-url @ENDPOINT@ < @PROMPT_FILE@

Placeholders (shell-quoted on substitution): @REPO@ @PROMPT_FILE@ @ENDPOINT@
@MODEL@ @RUN_DIR@ @ROLE@.  The command runs with @REPO@ as its working directory
and must exit when the agent is done.  Agents whose MCP configuration is global
(not per invocation) declare optional AGENT_SETUP_<NAME> / AGENT_TEARDOWN_<NAME>
commands; the runner executes them before and after the run.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

BUILTIN = ("codex", "claude", "agy")
CUSTOM_PREFIX = "AGENT_CMD_"


def q(value: str | Path) -> str:
    return shlex.quote(str(value))


def custom_agents(config: dict[str, str]) -> dict[str, str]:
    """Extract {name: template} from AGENT_CMD_<NAME> config keys."""
    return {
        key[len(CUSTOM_PREFIX):].lower(): value
        for key, value in config.items()
        if key.startswith(CUSTOM_PREFIX) and value
    }


def hooks_for(config: dict[str, str], name: str, kind: str) -> str:
    """Optional global-config hook: kind is SETUP or TEARDOWN."""
    return config.get(f"AGENT_{kind}_{name.upper()}", "")


def validate(agent_type: str, custom: dict[str, str]) -> None:
    if agent_type not in BUILTIN and agent_type not in custom:
        raise RuntimeError(
            f"unknown agent '{agent_type}': built-ins are {', '.join(BUILTIN)}; "
            f"define AGENT_CMD_{agent_type.upper()} in the config for others"
        )


def expand(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(f"@{key}@", q(value))
    return template


def build_command(
    agent_type: str,
    *,
    role: str,
    repo: Path,
    prompt: Path,
    endpoint: str,
    model: str,
    run_dir: Path,
    custom: dict[str, str],
) -> str:
    if agent_type == "codex":
        parts = [
            "codex", "--dangerously-bypass-approvals-and-sandbox", "exec", "--json", "--cd", str(repo),
            "-c", f"mcp_servers.lain.url={json.dumps(endpoint)}",
        ]
        if model:
            parts.extend(["--model", model])
        return " ".join(q(part) for part in parts) + " - < " + q(prompt)
    if agent_type == "claude":
        mcp = json.dumps({"mcpServers": {"lain": {"type": "http", "url": endpoint}}})
        parts = [
            "claude", "--print", "--verbose", "--output-format", "stream-json",
            "--include-hook-events", "--dangerously-skip-permissions",
            "--strict-mcp-config", "--mcp-config", mcp,
        ]
        if model:
            parts.extend(["--model", model])
        return "cd " + q(repo) + " && " + " ".join(q(part) for part in parts) + " < " + q(prompt)
    if agent_type == "agy":
        parts = [
            "agy", "--output-format", "stream-json",
            "--dangerously-skip-permissions", "--print-timeout", "600s",
            "--log-file", str(run_dir / "artifacts" / "raw" / f"{role}-agy.internal.log"),
        ]
        if model:
            parts.extend(["--model", model])
        parts.append("--print=" + prompt.read_text())
        return "cd " + q(repo) + " && " + " ".join(q(part) for part in parts)
    if agent_type in custom:
        body = expand(custom[agent_type], {
            "REPO": str(repo), "PROMPT_FILE": str(prompt), "ENDPOINT": endpoint,
            "MODEL": model, "RUN_DIR": str(run_dir), "ROLE": role,
        })
        return "cd " + q(repo) + " && " + body
    raise RuntimeError(f"unsupported agent type: {agent_type}")
