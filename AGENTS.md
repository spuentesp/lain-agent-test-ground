# Instructions for agents in this test ground

- Do not treat anything under `tests/` as trustworthy test evidence. It is legacy
  output from an invalid experiment.
- Never edit a source checkout named by `TAURI_SOURCE` or `PLUGINS_SOURCE`; runs use
  disposable clones below `runs/`.
- Do not report a run as passing. Only `harness/verify.py` determines the verdict.
- Never publish raw `.lain/events.jsonl` or raw agent transcripts. They can contain
  session credentials; publish only the sanitized files under `artifacts/share/`.
