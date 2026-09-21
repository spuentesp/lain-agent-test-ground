#!/usr/bin/env python3
"""Continuously display sanitized LAIN and Git activity for a run."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path


TOKEN = re.compile(r'("session_token"\s*:\s*")[^"]+("|$)')


def main() -> None:
    run_dir = Path(sys.argv[1]).resolve()
    meta = json.loads((run_dir / "run.json").read_text())
    offsets: dict[Path, int] = {}
    print(f"Observing LAIN run {meta['run_id']}", flush=True)
    while True:
        shared = meta.get("shared_lain")
        if shared:
            event_files = list(Path(shared["state_home"]).glob("lain/federation/*/.lain/events.jsonl"))
        else:
            event_files = [Path(value["path"]) / ".lain" / "events.jsonl" for value in meta["repositories"].values()]
        for path in event_files:
            if not path.exists():
                continue
            offsets.setdefault(path, 0)
            with path.open(errors="replace") as stream:
                stream.seek(offsets[path])
                for line in stream:
                    clean = TOKEN.sub(r'\1<redacted>\2', line.rstrip())
                    print(f"[{path.parent.parent.name}] {clean}", flush=True)
                offsets[path] = stream.tell()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
