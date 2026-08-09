---
name: env-report
description: Checks that the agent container is wired up correctly and writes a short Markdown health report. Use when asked to run a health check, a smoke test, a connectivity check, or to report on the container environment.
---

# Environment Report

A smoke test: confirm the container can run a command, then report what it found.
Keep this cheap — one tool call, one short report.

## Workflow

1. Run the collector script exactly once. Do not run any other command:

   ```
   bash /app/bin/collect-env.sh
   ```

2. Emit the report below as your final message. Do not re-run the script, do
   not read any files, and do not investigate anything further.

## Report format

Your final message must be exactly this Markdown and nothing else — no
preamble, no closing offer to help.

```markdown
# Agent Container Health Check

| Check | Value |
|-------|-------|
| OS / arch | ... |
| Python | ... |
| git | ... |
| Working directory | ... |
| Writable output dir | ... |

**Verdict:** one sentence — `healthy` if every check returned a value, or
`degraded` naming the checks that failed.
```

Copy the values straight from the script output. If a line reports an error,
put the error text in the table rather than omitting the row.
