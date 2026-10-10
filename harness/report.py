#!/usr/bin/env python3
"""Aggregate scenario verdicts into one markdown battery report.

    ./harness/report.py [--runs runs] [--out reports/battery.md]

Uses the newest verdict per scenario.  Failures are grouped by category so a
reader can tell a LAIN defect (lain) from agent disobedience (agent) from an
environment/code problem (code).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def newest_verdicts(runs: Path) -> list[dict]:
    latest: dict[str, tuple[float, dict]] = {}
    for path in runs.glob("*/artifacts/scenario-verdict.json"):
        data = json.loads(path.read_text())
        sid = data["scenario"]["id"]
        if sid not in latest or path.stat().st_mtime > latest[sid][0]:
            latest[sid] = (path.stat().st_mtime, data)
    return [latest[key][1] for key in sorted(latest)]


def mark(ok: bool) -> str:
    return "PASS" if ok else "**FAIL**"


def render(verdicts: list[dict]) -> str:
    lines = ["# LAIN team-scenario battery", ""]
    versions = {v.get("versions", {}).get("lain", "?") for v in verdicts}
    lines += [f"LAIN: {', '.join(sorted(versions))}", "",
              "| Scenario | Agents | Overall | LAIN | Agent | Code |", "|---|---|---|---|---|---|"]
    for v in verdicts:
        agents = ", ".join(f"{r}={t}" for r, t in v.get("agent_types", {}).items()) or "stdio processes"
        lines.append(f"| {v['scenario']['id']} {v['scenario']['title']} | {agents} | {mark(v['passed'])} | "
                     f"{mark(v['lain_passed'])} | {mark(v['agent_passed'])} | {mark(v['code_passed'])} |")
    for v in verdicts:
        failed = [c for c in v["checks"] if not c["passed"]]
        lines += ["", f"## {v['scenario']['id']} — {v['scenario']['title']}", "", v["scenario"]["intent"], ""]
        if not failed:
            lines.append("All checks passed.")
        for c in failed:
            lines.append(f"- FAIL ({c['category']}) {c['name']} — {c['detail']}")
        lines.append(f"\nRun: `{v['run_dir']}`")
    lines += ["", "Category key: `lain` = LAIN event-log evidence; `agent` = agent obeyed the protocol; `code` = harness-rerun verification."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, default=ROOT / "runs")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "battery.md")
    args = parser.parse_args()
    verdicts = newest_verdicts(args.runs)
    if not verdicts:
        print("no scenario verdicts found")
        return 2
    text = render(verdicts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)
    return 0 if all(v["passed"] for v in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
