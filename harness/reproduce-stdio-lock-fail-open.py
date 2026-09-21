#!/usr/bin/env python3
"""Reproduce cross-process LAIN claim failures with real stdio MCP servers.

Exit codes:
  0: no invariant violation was observed
  1: multiple processes received the same edit claim
  2: the reproducer itself failed
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


class McpProcess:
    def __init__(self, binary: Path, workspace: Path, state_home: Path, log: Path, label: str):
        env = os.environ.copy()
        env.update(
            {
                "XDG_STATE_HOME": str(state_home),
                "LAIN_REINDEX_TIMEOUT": "30",
            }
        )
        env.pop("LAIN_EMBEDDING_MODEL", None)
        self.log_stream = log.open("w")
        self.process = subprocess.Popen(
            [str(binary), "mcp", "--workspace", str(workspace)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log_stream,
            text=True,
            bufsize=1,
            env=env,
        )
        self.label = label
        self.next_id = 1
        self.agent_id = ""
        self.session_token = ""

    def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError(f"{self.label}: MCP pipes unavailable")
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(f"{self.label}: MCP server closed stdout during {method}")
            response = json.loads(line)
            if response.get("id") == request_id:
                return response

    def initialize(self, protocol_version: str) -> None:
        response = self.send(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "lain-lock-reproducer", "version": "1"},
            },
        )
        if "result" not in response:
            raise RuntimeError(f"{self.label}: initialize failed: {response}")
        if self.process.stdin is None:
            raise RuntimeError(f"{self.label}: MCP stdin unavailable")
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        self.process.stdin.flush()

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.send("tools/call", {"name": tool, "arguments": arguments})
        if "error" in response:
            return {"rpc_error": response["error"]}
        result = response.get("result", {})
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""
        if result.get("isError"):
            return {"tool_error": text}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"unparsed": text}

    def register(self) -> None:
        result = self.call(
            "register_agent",
            {"name": self.label, "kind": "other", "mode": "interactive"},
        )
        self.agent_id = result.get("agent_id", "")
        self.session_token = result.get("session_token", "")
        if not self.agent_id or not self.session_token:
            raise RuntimeError(f"{self.label}: registration failed: {result}")

    def claim(self, path: str, barrier: threading.Barrier) -> dict[str, Any]:
        barrier.wait()
        started = time.monotonic()
        result = self.call(
            "claim_files",
            {
                "agent_id": self.agent_id,
                "session_token": self.session_token,
                "files": [{"path": path, "intent": "edit", "ttl_seconds": 120}],
            },
        )
        return {
            "agent": self.label,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "granted": [item.get("path") for item in result.get("granted", [])],
            "conflicts": [
                {"agent_id": item.get("agent_id"), "path": item.get("path")}
                for item in result.get("conflicts", [])
            ],
            "error": result.get("tool_error") or result.get("rpc_error"),
        }

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.log_stream.close()


def run_checked(command: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def build_workspace(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "src").mkdir()
    (path / "Cargo.toml").write_text(
        '[package]\nname = "lain_lock_repro"\nversion = "0.1.0"\nedition = "2024"\n'
    )
    (path / "src" / "lib.rs").write_text("pub fn marker() -> u8 { 1 }\n")
    run_checked(["git", "init", "-q"], path)
    run_checked(["git", "config", "user.name", "LAIN lock reproducer"], path)
    run_checked(["git", "config", "user.email", "lain-repro@localhost"], path)
    run_checked(["git", "add", "-A"], path)
    run_checked(["git", "commit", "-q", "-m", "fixture"], path)


def protocol_version(binary: Path) -> str:
    value = run_checked([str(binary), "--print-mcp-protocol-version"])
    if not value:
        raise RuntimeError("installed lain returned an empty MCP protocol version")
    return value


def find_state_file(state_home: Path) -> Path:
    candidates = []
    for path in (state_home / "lain").glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "sessions" in data and "occupancy_by_file" in data:
            candidates.append(path)
    if len(candidates) != 1:
        raise RuntimeError(f"expected one presence state file, found: {candidates}")
    return candidates[0]


def one_iteration(
    binary: Path,
    protocol: str,
    output: Path,
    iteration: int,
    agent_count: int,
    force_fail_open: bool,
    padding_mib: int,
) -> dict[str, Any]:
    iteration_dir = output / f"iteration-{iteration:03d}"
    workspace = iteration_dir / "workspace"
    state_home = iteration_dir / "state"
    logs = iteration_dir / "logs"
    logs.mkdir(parents=True)
    state_home.mkdir()
    build_workspace(workspace)

    clients: list[McpProcess] = []
    lock_path: Path | None = None
    try:
        for index in range(agent_count):
            client = McpProcess(
                binary,
                workspace,
                state_home,
                logs / f"agent-{index:02d}.stderr.log",
                f"repro-{iteration:03d}-{index:02d}",
            )
            client.initialize(protocol)
            client.register()
            clients.append(client)

        state_file = find_state_file(state_home)
        if force_fail_open:
            # Slow the unlocked refresh enough that peers overlap while
            # reading the same pre-claim snapshot. JSON permits trailing
            # whitespace; the next persistence write compacts it again.
            with state_file.open("ab") as stream:
                chunk = b" " * (1024 * 1024)
                for _ in range(padding_mib):
                    stream.write(chunk)
            lock_path = state_file.with_name(state_file.name + ".lock")
            # A fresh sentinel remains non-stale for ten seconds. Every claim
            # waits for the two-second acquisition deadline, then proceeds
            # unlocked at nearly the same instant.
            lock_path.write_text(f"forced-by-reproducer pid={os.getpid()}\n")

        barrier = threading.Barrier(agent_count)
        with concurrent.futures.ThreadPoolExecutor(max_workers=agent_count) as pool:
            futures = [pool.submit(client.claim, "src/lib.rs", barrier) for client in clients]
            results = [future.result(timeout=30) for future in futures]

        grants = sum(bool(result["granted"]) for result in results)
        conflicts = sum(bool(result["conflicts"]) for result in results)
        errors = sum(result["error"] is not None for result in results)
        return {
            "iteration": iteration,
            "forced_lock": force_fail_open,
            "state_padding_mib": padding_mib if force_fail_open else 0,
            "state_file": str(state_file),
            "lock_file": str(lock_path) if lock_path else None,
            "grant_count": grants,
            "conflict_count": conflicts,
            "error_count": errors,
            "invariant_violated": grants > 1,
            "results": results,
        }
    finally:
        if lock_path is not None:
            lock_path.unlink(missing_ok=True)
        for client in clients:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Race real lain stdio MCP processes for one edit claim."
    )
    parser.add_argument(
        "--lain-bin",
        type=Path,
        default=Path("/home/spuentesp/.lain/bin/lain"),
        help="installed lain executable to test",
    )
    parser.add_argument("--agents", type=int, default=8, help="processes per iteration")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--padding-mib",
        type=int,
        default=64,
        help="trailing JSON whitespace used to widen the forced stale-read window",
    )
    parser.add_argument(
        "--natural",
        action="store_true",
        help="do not force the state-lock timeout; probe only natural contention",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="artifact directory (default: runs/stdio-lock-repro-<timestamp>)",
    )
    args = parser.parse_args()
    if args.agents < 2 or args.iterations < 1 or args.padding_mib < 0:
        parser.error("--agents must be >= 2, --iterations >= 1, and --padding-mib >= 0")

    binary = args.lain_bin.expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(f"not an executable: {binary}")
    output = (
        args.output.resolve()
        if args.output
        else ROOT / "runs" / time.strftime("stdio-lock-repro-%Y%m%d-%H%M%S")
    )
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)

    version = run_checked([str(binary), "--version"])
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    protocol = protocol_version(binary)
    iterations = []
    for number in range(1, args.iterations + 1):
        result = one_iteration(
            binary,
            protocol,
            output,
            number,
            args.agents,
            not args.natural,
            args.padding_mib,
        )
        iterations.append(result)
        marker = "VIOLATION" if result["invariant_violated"] else "ok"
        print(
            f"[{marker}] iteration={number} grants={result['grant_count']} "
            f"conflicts={result['conflict_count']} errors={result['error_count']}",
            flush=True,
        )

    reproduced = any(item["invariant_violated"] for item in iterations)
    report = {
        "schema_version": 1,
        "binary": str(binary),
        "binary_version": version,
        "binary_sha256": digest,
        "protocol_version": protocol,
        "agents_per_iteration": args.agents,
        "forced_lock": not args.natural,
        "state_padding_mib": args.padding_mib if not args.natural else 0,
        "issue_reproduced": reproduced,
        "iterations": iterations,
    }
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"report={report_path}")
    print("ISSUE REPRODUCED" if reproduced else "issue not observed")
    return 1 if reproduced else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, concurrent.futures.TimeoutError) as error:
        print(f"reproducer failed: {error}", file=sys.stderr)
        raise SystemExit(2)
