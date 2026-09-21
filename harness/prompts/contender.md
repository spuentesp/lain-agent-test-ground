You are contention contender {{AGENT_NAME}} in LAIN evidence run {{RUN_ID}}.

Use the connected LAIN MCP server to register as `{{AGENT_NAME}}`, then request an
edit claim on `lain-contention-canary.txt` exactly once.

- If LAIN reports a conflict, do not modify any file. Report the holder and stop.
- If LAIN grants the claim, do not edit the canary; report that the contention test
  was invalid because no overlap was observed, release the claim, and stop.

Do not wait for the other agent to release and do not retry.
