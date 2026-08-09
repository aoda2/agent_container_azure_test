---
name: code-review
description: Reviews a Git diff for correctness bugs, security issues, and maintainability problems, then writes a structured Markdown review report. Use when asked to review code, review a pull request, review a diff, review changes, or audit a commit range in a Git repository.
---

# Code Review

Review a Git diff and produce a single Markdown report. You are reviewing
changes, not the whole codebase — read surrounding code only when the diff
alone is not enough to judge a change.

## Workflow

1. **Establish the range.** You will be told a base ref and a head ref.
   Confirm they exist before diffing:

   ```
   git rev-parse --verify <ref>
   ```

   If the base ref does not resolve, fall back to `HEAD~1`, and say so in the
   report's Scope section.

2. **Get the shape of the change first**, so you know how much there is to read:

   ```
   git diff --stat <base>..<head>
   ```

3. **Read the diff**, one file at a time for anything non-trivial:

   ```
   git diff <base>..<head> -- <path>
   ```

4. **Read surrounding code when the diff is ambiguous.** A changed line often
   only makes sense next to the function it lives in. Use `Read` on the file,
   and `Grep` to find callers of a changed function or usages of a changed
   field.

5. **Write the report** in the exact format below as your final message.

## What to look for

Work through these in order. Correctness first — a style note is never more
important than a bug.

- **Correctness** — off-by-one errors, inverted conditions, unhandled `None`
  or empty collections, resource leaks, incorrect error handling, race
  conditions, changes that break an existing caller.
- **Security** — hardcoded credentials or tokens, unvalidated input reaching a
  shell/SQL/path operation, missing authorization checks, secrets written to
  logs.
- **Tests** — new behavior with no test, a test changed to match a bug rather
  than the intent, an assertion weakened or deleted.
- **Maintainability** — duplicated logic that already exists elsewhere in the
  repo, a function that no longer matches its name or docstring, a public
  signature changed without updating call sites.

## Rules for findings

- **Every finding needs evidence.** Cite `path/to/file.py:42`. If you cannot
  point at a line, it is not a finding.
- **Every finding needs a failure scenario** — concrete inputs or state that
  lead to a wrong result. "This could be fragile" is not a finding.
- **Only review changed lines** and code whose behavior the change alters.
  Pre-existing issues in untouched code are out of scope; mention at most one
  in Notes if it is severe.
- **Report zero findings when there are none.** An empty Findings section is a
  valid, useful result. Do not pad the report to look thorough.

## Severity

| Severity | Meaning |
|----------|---------|
| `high`   | Causes incorrect behavior, data loss, or a security hole in normal use |
| `medium` | Breaks under a plausible edge case, or is a real maintainability problem |
| `low`    | Worth fixing but harmless — naming, dead code, a missing test for a minor path |

## Report format

Your final message must be exactly this Markdown document and nothing else —
no preamble, no "here is the report", no closing offer to help.

```markdown
# Code Review

**Scope:** `<base>..<head>` — N files changed, +X / -Y lines

## Summary

One paragraph: what the change does, and whether it is safe to merge.

## Findings

### 1. <short title> — `high`

`path/to/file.py:42`

What is wrong, and the concrete scenario in which it fails.

**Suggested fix:** one or two sentences.

### 2. <short title> — `medium`

...

## Notes

Observations that are not defects — context worth knowing, or a follow-up
worth filing. Omit this section if there is nothing to say.
```

If there are no findings, replace the Findings section body with a single
line: `No issues found in the reviewed range.`
