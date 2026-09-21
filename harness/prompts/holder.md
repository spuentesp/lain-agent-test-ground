You are contention holder {{AGENT_NAME}} in LAIN evidence run {{RUN_ID}}.

Use the connected LAIN MCP server to register as `{{AGENT_NAME}}`. Claim
`lain-contention-canary.txt` for edit. If and only if it is granted, replace the
file contents with exactly:

holder={{AGENT_NAME}}
run={{RUN_ID}}

Keep the claim for at least 30 seconds so another real agent can contend for it.
Then commit the file with subject `lain-eval({{AGENT_NAME}}): hold canary`, release
the claim, and report the tool calls you made. Do not modify any other file.
