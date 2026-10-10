#!/usr/bin/env python3
"""Run one team scenario with real agent processes and write a categorized verdict.

    ./harness/run-scenario.py S3 --config harness/config.env

Flow: prepare disposable clones -> shared LAIN HTTP server in tmux -> execute the
scenario timeline (agents are real OS processes in tmux windows) -> harness reruns
the verification command -> evidence checks -> artifacts/scenario-verdict.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import agents
import evidence as ev
import lainlog
import prepare
import scenarios

HARNESS = Path(__file__).resolve().parent
ROOT = HARNESS.parent
POLL = 1.0


def cargo_target(cfg: dict[str, str]) -> Path:
    """One shared build cache for every clone and run: cuts disk use and recompiles."""
    return Path(cfg.get("CARGO_TARGET_DIR") or ROOT / "runs" / ".cargo-target")


class ScenarioError(RuntimeError):
    """A timeline step could not complete; recorded, evidence stage still runs."""


def q(value) -> str:
    return shlex.quote(str(value))


def tmux(*args: str, check: bool = True) -> str:
    done = subprocess.run(["tmux", *args], text=True, capture_output=True)
    if check and done.returncode != 0:
        raise ScenarioError(f"tmux {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


# ------------------------------------------------------------------ session
class Session:
    """tmux-backed runtime for one scenario: one shared LAIN server + agent windows."""

    def __init__(self, meta: dict, cfg: dict[str, str]) -> None:
        self.meta, self.cfg = meta, cfg
        self.run_dir = Path(meta["run_dir"])
        self.raw = self.run_dir / "artifacts" / "raw"
        self.status = self.run_dir / "artifacts" / "status"
        self.name = f"lain-{meta['run_id']}"
        self.server_starts = 0

    # -- server ------------------------------------------------------------
    def start_server(self) -> None:
        shared = self.meta["shared_lain"]
        profile = self.cfg.get("LAIN_TOOL_PROFILE", "session,social,notes,federation")
        command = (f"XDG_STATE_HOME={q(shared['state_home'])} LAIN_TOOL_PROFILE={q(profile)} lain server --config {q(shared['config'])} "
                   f"--transport http --port {shared['port']} --log-level info")
        window = "lain-server" if self.server_starts == 0 else f"lain-server-{self.server_starts + 1}"
        if self.server_starts == 0:
            tmux("new-session", "-d", "-s", self.name, "-n", window, f"bash -lc {q(command)}")
        else:
            tmux("new-window", "-d", "-t", self.name, "-n", window, f"bash -lc {q(command)}")
        self.server_starts += 1
        self._capture(window, "lain-server")
        for _ in range(240):
            with socket.socket() as probe:
                probe.settimeout(0.25)
                if probe.connect_ex(("127.0.0.1", shared["port"])) == 0:
                    return
            time.sleep(0.5)
        raise ScenarioError("shared LAIN server did not become ready")

    def kill_server(self) -> None:
        for log in lainlog.live_logs(self.meta):  # the restarted server discards this file
            shutil.copy(log, self.raw / f"lain-events-precrash-{len(list(self.raw.glob('lain-events-precrash-*')))}.jsonl")
        # Anchored: an unanchored pattern also matches the tmux server's own argv.
        subprocess.run(["pkill", "-KILL", "-f", f"^lain server --config {self.meta['shared_lain']['config']}"], check=False)
        time.sleep(1)

    def _capture(self, window: str, log_name: str) -> None:
        log = self.raw / f"{log_name}.log"
        tmux("pipe-pane", "-o", "-t", f"{self.name}:{window}", f"exec stdbuf -oL tee -a {q(log)}")
        tmux("set-option", "-w", "-t", f"{self.name}:{window}", "remain-on-exit", "on")

    # -- agents ------------------------------------------------------------
    def start(self, role: str) -> None:
        meta = self.meta
        agent_type = meta["agent_types"][role]
        command = agents.build_command(
            agent_type, role=role, repo=Path(meta["repositories"][meta["role_repos"][role]]["path"]),
            prompt=self.run_dir / "prompts" / f"{role}.md", endpoint=meta["shared_lain"]["endpoint"],
            model=meta["models"].get(agent_type, ""), run_dir=self.run_dir, custom=meta["custom_agents"],
        )
        gate = self.status / f"{role}.start"
        wrapped = (f"export CARGO_TARGET_DIR={q(cargo_target(self.cfg))}; "
                   f"while [[ ! -f {q(gate)} ]]; do sleep 0.05; done; {command}; code=$?; "
                   f"printf '%s\\n' \"$code\" > {q(self.status / (role + '.exit'))}; exit \"$code\"")
        tmux("new-window", "-d", "-t", self.name, "-n", role, f"bash -lc {q(wrapped)}")
        self._capture(role, role)
        gate.touch()
        print(f"  started {role} ({agent_type})", flush=True)

    def kill(self, role: str) -> None:
        """SIGKILL the agent's descendants; the tmux wrapper still records the exit code."""
        pid = tmux("display-message", "-p", "-t", f"{self.name}:{role}", "#{pane_pid}")
        victims: list[str] = []
        frontier = [pid]
        while frontier:
            children = [c for p in frontier for c in subprocess.run(
                ["pgrep", "-P", p], text=True, capture_output=True).stdout.split()]
            victims.extend(children)
            frontier = children
        if not victims:
            raise ScenarioError(f"no process to kill for {role}")
        subprocess.run(["kill", "-KILL", *victims], check=False)
        print(f"  SIGKILL {role} ({len(victims)} processes)", flush=True)

    # -- waiting -------------------------------------------------------------
    def wait_for(self, what: str, predicate, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(POLL)
        raise ScenarioError(f"timeout ({timeout:.0f}s) waiting for {what}")

    def _agent_events(self, role: str, name: str):
        events = lainlog.all_events(self.meta)
        ids = lainlog.agent_ids(events, self.meta["agents"][role])
        return [e for e in events if lainlog.actor(e, name) in ids]

    def wait_claim(self, role: str, suffix: str | None, timeout: float = 600) -> None:
        def granted() -> bool:
            return any(suffix is None or str(e["ClaimGranted"]["path"]).endswith(suffix)
                       for e in self._agent_events(role, "ClaimGranted"))
        self.wait_for(f"{role} claim on {suffix or 'any file'}", granted, timeout)

    def wait_edit(self, role: str, timeout: float = 900) -> None:
        self.wait_for(f"attributed edit by {role}", lambda: bool(self._agent_events(role, "EditLanded")), timeout)

    def wait_exit(self, role: str, timeout: float) -> None:
        self.wait_for(f"{role} to exit", lambda: (self.status / f"{role}.exit").exists(), timeout)

    def wal_size(self) -> None:
        total = sum(p.stat().st_size for p in self.run_dir.rglob("*.wal"))
        (self.status / "wal-before-kill.txt").write_text(f"{total}\n")
        print(f"  WAL bytes before crash: {total}", flush=True)

    # -- timeline ------------------------------------------------------------
    def execute(self, step: tuple) -> None:
        op, *args = step
        print(f"[step] {step}", flush=True)
        {
            "start": self.start, "kill": self.kill, "wait_claim": self.wait_claim,
            "wait_edit": self.wait_edit, "wait_exit": self.wait_exit,
            "sleep": lambda seconds: time.sleep(seconds), "wal_size": self.wal_size,
            "server_kill": self.kill_server, "server_start": self.start_server,
        }[op](*args)

    def stop(self) -> None:
        for index, log in enumerate(lainlog.live_logs(self.meta)):
            shutil.copy(log, self.raw / f"lain-events-final-{index}.jsonl")
        subprocess.run(["pkill", "-KILL", "-f", f"^lain server --config {self.meta['shared_lain']['config']}"], check=False)
        tmux("kill-session", "-t", self.name, check=False)  # windows' pipe-pane logs are already on disk


# ------------------------------------------------------------------ run setup
def assign_agents(cfg: dict[str, str], scenario: scenarios.Scenario) -> dict[str, str]:
    pool = [a.strip() for a in cfg.get("TEAM_AGENTS", "codex,claude,agy,kimi,minimax").split(",") if a.strip()]
    offset = int(scenario.id[1:])
    return {
        role.key: cfg.get(f"{scenario.id}_{role.key}_AGENT") or pool[(offset + position) % len(pool)]
        for position, role in enumerate(scenario.roles)
    }


def build_run(cfg: dict[str, str], scenario: scenarios.Scenario) -> dict:
    served = ("tauri", "plugins") if any(role.repo == "plugins" for role in scenario.roles) else ("tauri",)
    meta = prepare.prepare_workspace(cfg, assign_agents(cfg, scenario), served)
    run_id, run_dir = meta["run_id"], Path(meta["run_dir"])
    meta["scenario"] = {"id": scenario.id, "title": scenario.title, "intent": scenario.intent}
    meta["agents"] = {r.key: f"{r.key}-{meta['agent_types'][r.key]}-{run_id}" for r in scenario.roles}
    meta["role_repos"] = {r.key: r.repo for r in scenario.roles}
    meta["role_kinds"] = {r.key: r.kind for r in scenario.roles}
    meta["verification"]["tauri"] = cfg.get("TEAM_VERIFY_CMD", scenarios.UTILS_VERIFY)
    meta["probe_symbol"] = "lain_wal_probe_" + re.sub(r"[^a-z0-9]", "_", run_id.lower())
    repo_names = {"tauri": "tauri-apps/tauri", "plugins": "tauri-apps/plugins-workspace"}
    for role in scenario.roles:
        text = role.brief.replace("{{PROBE}}", meta["probe_symbol"])
        prepare.render(HARNESS / "prompts" / "agent.md", run_dir / "prompts" / f"{role.key}.md", {
            "AGENT_NAME": meta["agents"][role.key], "RUN_ID": run_id,
            "REPOSITORY": repo_names[role.repo], "BRIEF": text,
        })
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


class AgentConfigGuard:
    """Point global-config agents (agy, or any with AGENT_SETUP_*) at this run; always restore."""

    def __init__(self, meta: dict, cfg: dict[str, str]) -> None:
        self.meta, self.cfg = meta, cfg
        self.raw = Path(meta["run_dir"]) / "artifacts" / "raw"
        self.agy_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
        self.used = set(meta["agent_types"].values())

    def __enter__(self):
        endpoint = self.meta["shared_lain"]["endpoint"]
        if "agy" in self.used:
            if self.agy_path.exists():
                shutil.copy(self.agy_path, self.raw / "agy-mcp-config.before.json")
            else:
                (self.raw / "agy-mcp-config.was-missing").touch()
            subprocess.run(["agy", "mcp", "add", "lain", endpoint], check=True)
        for name in self.used - set(agents.BUILTIN):
            command = agents.hooks_for(self.cfg, name, "SETUP")
            if command:
                subprocess.run(["bash", "-lc", agents.expand(command, {"ENDPOINT": endpoint})], check=True)
        return self

    def __exit__(self, *exc):
        before = self.raw / "agy-mcp-config.before.json"
        if "agy" in self.used:
            if before.exists():
                shutil.copy(before, self.agy_path)
            else:
                subprocess.run(["agy", "mcp", "remove", "lain"], check=False)
        for name in self.used - set(agents.BUILTIN):
            command = agents.hooks_for(self.cfg, name, "TEARDOWN")
            if command:
                subprocess.run(["bash", "-lc", agents.expand(command, {"ENDPOINT": self.meta["shared_lain"]["endpoint"]})], check=False)


def run_verification(meta: dict, repos: tuple[str, ...], cfg: dict[str, str]) -> None:
    run_dir = Path(meta["run_dir"])
    for key in repos:
        log = run_dir / "artifacts" / "raw" / f"verify-{key}.log"
        command = meta["verification"][key]
        print(f"[verify] {key}: {command}", flush=True)
        with log.open("w") as stream:
            stream.write(f"$ {command}\n")
            stream.flush()
            done = subprocess.run(["bash", "-o", "pipefail", "-lc", command], cwd=meta["repositories"][key]["path"],
                                  stdout=stream, stderr=subprocess.STDOUT,
                                  env={**os.environ, "CARGO_TARGET_DIR": str(cargo_target(cfg))})
        (run_dir / "artifacts" / "status" / f"verify-{key}.exit").write_text(f"{done.returncode}\n")


def publish_share(meta: dict) -> None:
    """Sanitized copy of logs/events (session tokens redacted) — the only thing to share."""
    run_dir = Path(meta["run_dir"])
    share = run_dir / "artifacts" / "share"
    share.mkdir(parents=True, exist_ok=True)
    events = lainlog.all_events(meta)
    secrets = {d["session_token"] for e in events if (d := e.get("AgentJoined")) and d.get("session_token")}
    token = re.compile(r'("session_token"\s*:\s*")[^"]+("|$)')

    def clean(text: str) -> str:
        text = token.sub(r'\1<redacted>\2', text)
        for secret in secrets:
            text = text.replace(secret, "<redacted>")
        return text

    (share / "events.sanitized.json").write_text(clean(json.dumps(events, indent=2)) + "\n")
    for log in (run_dir / "artifacts" / "raw").glob("*.log"):
        (share / log.name).write_text(clean(log.read_text(errors="replace")))


def print_verdict(verdict: dict) -> None:
    for item in verdict["checks"]:
        print(f"[{'PASS' if item['passed'] else 'FAIL'}] ({item['category']}) {item['name']}: {item['detail']}")
    flags = " ".join(f"{c}={'ok' if verdict[f'{c}_passed'] else 'FAIL'}" for c in ev.CATEGORIES)
    print(f"\n{verdict['scenario']['id']} {verdict['scenario']['title']}: {'PASS' if verdict['passed'] else 'FAIL'}  [{flags}]")


# ------------------------------------------------------------------ S6 (script)
def run_stdio_probe(cfg: dict[str, str]) -> dict:
    """Wraps the known-issue reproducer; passes only when no double grant is observed."""
    lain_bin = shutil.which("lain")
    run_dir = ROOT / "runs" / ("S6-" + time.strftime("%Y%m%d-%H%M%S"))
    (run_dir / "artifacts").mkdir(parents=True)
    checks = []
    for label, extra in (("forced lock timeout", []), ("natural race", ["--natural"])):
        output = run_dir / label.split()[0]
        done = subprocess.run([sys.executable, str(HARNESS / "reproduce-stdio-lock-fail-open.py"), "--lain-bin", lain_bin,
                               "--agents", cfg.get("S6_AGENTS", "8"), "--iterations", cfg.get("S6_ITERATIONS", "3"),
                               "--output", str(output), *extra], text=True, capture_output=True)
        (run_dir / "artifacts" / f"{label.split()[0]}.log").write_text(done.stdout + done.stderr)
        verdict = {0: "not observed", 1: "ISSUE REPRODUCED (known)", 2: "reproducer error"}.get(done.returncode, "?")
        checks.append({"category": "lain", "name": f"S6 {label}: single edit grant per claim race",
                       "passed": done.returncode == 0, "detail": f"exit={done.returncode} {verdict}"})
    flags = {f"{c}_passed": all(x["passed"] for x in checks if x["category"] == c) for c in ev.CATEGORIES}
    result = {"scenario": {"id": "S6", "title": scenarios.SCRIPTS["S6"], "intent": "regression probe for the stdio state-lock fail-open path"},
              "run_dir": str(run_dir), **flags, "passed": all(flags.values()), "checks": checks,
              "versions": {"lain": subprocess.run([lain_bin, "--version"], text=True, capture_output=True).stdout.strip()}}
    (run_dir / "artifacts" / "scenario-verdict.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


# ------------------------------------------------------------------ main
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", help="S1..S7")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--keep-session", action="store_true", help="leave tmux windows up for inspection")
    args = parser.parse_args()
    cfg = prepare.parse_env(args.config.resolve())

    if args.scenario == "S6":
        verdict = run_stdio_probe(cfg)
        print_verdict(verdict)
        return 0 if verdict["passed"] else 1
    scenario = scenarios.SCENARIOS.get(args.scenario)
    if scenario is None:
        raise RuntimeError(f"unknown scenario {args.scenario}; choose from {', '.join([*scenarios.SCENARIOS, 'S6'])}")

    meta = build_run(cfg, scenario)
    run_dir = Path(meta["run_dir"])
    print(f"== {scenario.id}: {scenario.title}\n   run: {run_dir}\n   agents: {meta['agent_types']}", flush=True)
    session, errors = Session(meta, cfg), []
    with AgentConfigGuard(meta, cfg):
        try:
            session.start_server()
            tmux("new-window", "-d", "-t", session.name, "-n", "observer",
                 f"bash -lc {q('python3 -u ' + q(HARNESS / 'observe.py') + ' ' + q(run_dir))}")
            session._capture("observer", "observer")
            print(f"   watch: tmux attach -t {session.name}", flush=True)
            for step in scenario.steps:
                try:
                    session.execute(step)
                except ScenarioError as error:
                    errors.append(f"{step}: {error}")
                    print(f"  !! {error}", flush=True)
                    if step[0] in ("start", "server_start"):
                        break
        finally:
            if not args.keep_session:
                session.stop()
    run_verification(meta, scenario.verify, cfg)
    ctx = ev.Ctx(meta)
    ctx.add("agent", "timeline completed without orchestration errors", not errors, "; ".join(errors) or "all steps ran")
    scenario.check(ctx)
    ev.check_verification(ctx, scenario.verify)
    verdict = {"scenario": meta["scenario"], "run_dir": str(run_dir), "agents": meta["agents"],
               "agent_types": meta["agent_types"], "versions": meta["versions"], **ev.summarize(ctx)}
    (run_dir / "artifacts" / "scenario-verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    publish_share(meta)
    print_verdict(verdict)
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"scenario failed closed: {error}", file=sys.stderr)
        raise SystemExit(2)
