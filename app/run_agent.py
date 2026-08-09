"""Container entrypoint: run an Agent Skill inside the container.

Reads its configuration from environment variables so the same image can be
driven by an Azure Container Apps Job, a local `docker run`, or CI.

Two tasks ship with the image, selected with TASK:

  env-report   (default) A smoke test. One tool call, a few cents at most.
               Confirms the skill is discovered, the agent loop runs, and the
               report reaches stdout.
  code-review  The realistic sample. Clones a repo and reviews a diff range.
               Costs real money — see the cost table in README.md.

Environment variables
---------------------
ANTHROPIC_API_KEY   (required) Anthropic API key.
TASK                Which task to run.             Default: env-report
MODEL               Model alias or id.             Default: per-task
MAX_TURNS           Agent turn cap.                Default: per-task
OUTPUT_DIR          If set, the report is also written here as review.md.
SETTING_SOURCES     Comma-separated skill sources. Default: user

code-review only:
TARGET_REPO_URL     Git URL to clone. If unset, WORKSPACE_DIR is reviewed as-is.
GIT_TOKEN           Optional token injected into an https:// clone URL.
BASE_REF            Base of the diff range.        Default: origin/HEAD
HEAD_REF            Head of the diff range.        Default: HEAD
CLONE_DEPTH         Shallow clone depth, 0 = full. Default: 50
REVIEW_PROMPT       Extra instructions appended to the task prompt.
WORKSPACE_DIR       Where the repo lives.          Default: /workspace/repo
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

COLLECT_ENV = "/app/bin/collect-env.sh"


@dataclass(frozen=True)
class Task:
    """A skill plus the permissions and budget it needs."""

    skill: str
    allowed_tools: list[str]
    model: str
    max_turns: int
    needs_repo: bool


TASKS: dict[str, Task] = {
    # Smoke test: the agent may run exactly one script and nothing else.
    "env-report": Task(
        skill="env-report",
        allowed_tools=["Skill", f"Bash(bash {COLLECT_ENV})", f"Bash({COLLECT_ENV})"],
        model="haiku",
        max_turns=6,
        needs_repo=False,
    ),
    # Real work: read-only inspection plus git for the diff. `Bash(git:*)` is a
    # permission rule — bash is available, but only for git subcommands.
    "code-review": Task(
        skill="code-review",
        allowed_tools=["Skill", "Read", "Grep", "Glob", "Bash(git:*)"],
        model="sonnet",
        max_turns=40,
        needs_repo=True,
    ),
}

SYSTEM_PROMPT = (
    "You are running unattended in a container. Nobody is watching, so never "
    "ask a question or offer to do follow-up work — complete the task and emit "
    "the report the skill describes, then stop."
)


def log(message: str) -> None:
    """Write a progress line to stderr so it never pollutes the report on stdout."""
    print(message, file=sys.stderr, flush=True)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log(f"[warn] {name}={raw!r} is not an integer; using {default}")
        return default


def setting_sources() -> list[str]:
    """Which filesystem sources the SDK loads skills and settings from.

    Defaults to "user" only. The image installs skills under ~/.claude/skills,
    and deliberately does *not* load "project": for code-review, cwd is a repo
    we just cloned, and a `.claude/` directory committed there would otherwise
    feed attacker-controlled instructions and settings into the agent.
    """
    raw = os.environ.get("SETTING_SOURCES", "user")
    return [s.strip() for s in raw.split(",") if s.strip()]


def authenticated_url(url: str, token: str) -> str:
    """Inject a token into an https clone URL, leaving other schemes alone."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return url
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, f"x-access-token:{token}@{host}", parts.path, parts.query, parts.fragment))


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def clone(url: str, dest: Path, depth: int) -> None:
    """Clone `url` into `dest`, replacing anything already there."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone"]
    if depth > 0:
        # A shallow clone still needs enough history for BASE_REF to resolve.
        cmd += ["--depth", str(depth), "--no-single-branch"]
    cmd += [url, str(dest)]

    log(f"[setup] cloning into {dest} (depth={depth or 'full'})")
    # Fail fast and loudly rather than letting the agent review an empty dir.
    subprocess.run(cmd, check=True, stdout=sys.stderr, stderr=sys.stderr)


def resolve_base_ref(repo: Path, requested: str) -> str:
    """Return `requested` if git can resolve it, else a sensible fallback."""
    for candidate in (requested, "origin/HEAD", "HEAD~1"):
        try:
            git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", cwd=repo)
        except subprocess.CalledProcessError:
            continue
        if candidate != requested:
            log(f"[setup] base ref {requested!r} not found; using {candidate!r}")
        return candidate
    raise SystemExit(f"[error] cannot resolve a base ref for the diff (tried {requested}, origin/HEAD, HEAD~1)")


def prepare_env_report() -> tuple[Path, str]:
    """Nothing to set up — the skill only runs the collector script."""
    return Path("/app"), "Run the container health check and report the result."


def prepare_code_review() -> tuple[Path, str]:
    """Clone the target repo and build the review prompt."""
    repo = Path(os.environ.get("WORKSPACE_DIR", "/workspace/repo"))
    repo_url = os.environ.get("TARGET_REPO_URL", "").strip()

    if repo_url:
        token = os.environ.get("GIT_TOKEN", "").strip()
        clone(
            authenticated_url(repo_url, token) if token else repo_url,
            repo,
            env_int("CLONE_DEPTH", 50),
        )
    elif not (repo / ".git").is_dir():
        raise SystemExit(f"[error] TARGET_REPO_URL is unset and {repo} is not a git repository")

    base = resolve_base_ref(repo, os.environ.get("BASE_REF", "origin/HEAD").strip() or "origin/HEAD")
    head = os.environ.get("HEAD_REF", "HEAD").strip() or "HEAD"
    log(f"[setup] reviewing {base}..{head} in {repo}")

    prompt = (
        f"Review the changes in this repository between `{base}` and `{head}`.\n"
        "Use the code-review skill and follow its report format exactly."
    )
    extra = os.environ.get("REVIEW_PROMPT", "").strip()
    if extra:
        prompt += f"\n\nAdditional instructions from the operator:\n{extra}"
    return repo, prompt


PREPARE = {"env-report": prepare_env_report, "code-review": prepare_code_review}


async def run_task(task: Task, cwd: Path, prompt: str) -> str:
    """Drive the agent and return the report text."""
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=SYSTEM_PROMPT,
        setting_sources=setting_sources(),
        skills=[task.skill],
        allowed_tools=task.allowed_tools,
        # Deny anything not pre-approved instead of prompting: there is no TTY
        # here, and a permission prompt would hang the job until it times out.
        permission_mode="dontAsk",
        model=os.environ.get("MODEL", "").strip() or task.model,
        max_turns=env_int("MAX_TURNS", task.max_turns),
    )

    transcript: list[str] = []
    result: ResultMessage | None = None

    # Never raise from inside this loop. `query()` is backed by an async
    # generator with background tasks (reading the CLI subprocess); breaking
    # out early via an exception can race its cleanup and surface as
    # "RuntimeError: aclose(): asynchronous generator is already running"
    # instead of the real error. Collect everything, let the generator finish
    # on its own (ResultMessage is always the last item), then decide after.
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    transcript.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    log(f"[agent] tool: {block.name}")
        elif isinstance(message, ResultMessage):
            result = message

    if result is None:
        raise RuntimeError("agent run produced no result message")

    log(f"[agent] finished: {result.subtype} ({result.num_turns} turns)")
    if result.total_cost_usd is not None:
        log(f"[agent] cost: ${result.total_cost_usd:.4f}")

    if result.is_error:
        # `is_error` can be True even when subtype is "success" — that
        # combination means the turn hit an API-level failure (429/500/529)
        # that the CLI otherwise recovered from. Surface every detail the SDK
        # gives us; any of these may be None depending on the failure mode.
        detail = "; ".join(result.errors) if result.errors else result.subtype
        if result.api_error_status is not None:
            detail += f" (HTTP {result.api_error_status})"
        if result.permission_denials:
            log(f"[agent] permission denials: {result.permission_denials}")
        raise RuntimeError(f"agent run failed: {detail}")

    # `result.result` holds the final assistant turn's text; fall back to the
    # last streamed text block if a given CLI version omits it.
    result_text = result.result if isinstance(result.result, str) else ""
    return (result_text or (transcript[-1] if transcript else "")).strip()


def publish(report: str) -> None:
    """Emit the report to stdout, and to OUTPUT_DIR when one is mounted."""
    print(report, flush=True)

    output_dir = os.environ.get("OUTPUT_DIR", "").strip()
    if not output_dir:
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("report.md", f"report-{stamp}.md"):
        (directory / name).write_text(report, encoding="utf-8")
    log(f"[output] wrote report to {directory}")


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("[error] ANTHROPIC_API_KEY is not set")
        return 2

    name = os.environ.get("TASK", "env-report").strip() or "env-report"
    task = TASKS.get(name)
    if task is None:
        log(f"[error] unknown TASK {name!r}; expected one of: {', '.join(TASKS)}")
        return 2

    log(f"[setup] task={name} skill={task.skill} model={os.environ.get('MODEL') or task.model}")
    cwd, prompt = PREPARE[name]()

    report = await run_task(task, cwd, prompt)
    if not report:
        log("[error] the agent produced no report")
        return 1

    publish(report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except subprocess.CalledProcessError as exc:
        log(f"[error] command failed ({exc.returncode}): {' '.join(exc.cmd)}")
        sys.exit(exc.returncode or 1)
    except RuntimeError as exc:
        log(f"[error] {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
