"""Container entrypoint: run an Agent Skill inside the container.

Reads its configuration from environment variables so the same image can be
driven by an Azure Container Apps Job, a local `docker run`, or CI.

Three tasks ship with the image, selected with TASK:

  env-report   (default) A smoke test. One tool call, a few cents at most.
               Confirms the skill is discovered, the agent loop runs, and the
               report reaches stdout.
  code-review  The realistic sample. Clones a repo and reviews a diff range.
               Costs real money — see the cost table in README.md.
  explain-code Clones a repo and explains what a diff range does and how it
               works, instead of reviewing it for issues.

Environment variables
---------------------
ANTHROPIC_API_KEY       (required) Anthropic API key.
TASK                    Which task to run.             Default: env-report
MODEL                   Model alias or id.             Default: per-task
MAX_TURNS               Agent turn cap.                Default: per-task
OUTPUT_DIR              If set, the report is also written here as review.md.
SETTING_SOURCES         Comma-separated skill sources. Default: user
RUN_ID                  Groups blob uploads from related runs (e.g. a
                        multi-model comparison). Default: current timestamp.
STORAGE_CONNECTION_STRING  Azure Storage connection string. If set together
                        with BLOB_CONTAINER, the report is also uploaded to
                        blob storage at {RUN_ID}/{MODEL}/{TASK}/report-*.md.
BLOB_CONTAINER          Blob container name for the upload above.
OTEL_CAPTURE            Set to 1 to capture Claude Code's OpenTelemetry logs and
                        metrics (OTLP/HTTP JSON) via an in-process receiver and
                        upload them as otel-*.jsonl next to the report.
OTEL_LOG_USER_PROMPTS   Set to 1 to include prompt text in captured events.

code-review and explain-code only:
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
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    "explain-code": Task(
        skill="explain-code",
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


def prepare_repo_and_refs() -> tuple[Path, str, str]:
    """Clone the target repo (if needed) and resolve the diff range."""
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
    return repo, base, head


def prepare_code_review() -> tuple[Path, str]:
    """Build the review prompt for the resolved diff range."""
    repo, base, head = prepare_repo_and_refs()
    log(f"[setup] reviewing {base}..{head} in {repo}")

    prompt = (
        f"Review the changes in this repository between `{base}` and `{head}`.\n"
        "Use the code-review skill and follow its report format exactly."
    )
    extra = os.environ.get("REVIEW_PROMPT", "").strip()
    if extra:
        prompt += f"\n\nAdditional instructions from the operator:\n{extra}"
    return repo, prompt


def prepare_explain_code() -> tuple[Path, str]:
    """Build the explanation prompt for the resolved diff range."""
    repo, base, head = prepare_repo_and_refs()
    log(f"[setup] explaining {base}..{head} in {repo}")

    prompt = (
        f"Explain the changes in this repository between `{base}` and `{head}`.\n"
        "Use the explain-code skill and follow its report format exactly."
    )
    extra = os.environ.get("REVIEW_PROMPT", "").strip()
    if extra:
        prompt += f"\n\nAdditional instructions from the operator:\n{extra}"
    return repo, prompt


PREPARE = {
    "env-report": prepare_env_report,
    "code-review": prepare_code_review,
    "explain-code": prepare_explain_code,
}


def resolved_model(task: Task) -> str:
    """The model this run actually uses: an explicit override, else the task default."""
    return os.environ.get("MODEL", "").strip() or task.model


class OtelCapture:
    """Minimal in-process OTLP/HTTP receiver that appends payloads to a JSONL file.

    Claude Code is pointed at it with OTEL_EXPORTER_OTLP_PROTOCOL=http/json, so
    each POST body (/v1/logs, /v1/metrics, /v1/traces) is already standard OTLP
    JSON. One line per request: {"path": ..., "received_at": ..., "body": ...}.
    No Collector is needed, and nothing touches the CLI's stdout (which the SDK
    uses for its own protocol, so the `console` exporter is not an option).
    """

    def __init__(self) -> None:
        handle, name = tempfile.mkstemp(prefix="otel-", suffix=".jsonl")
        os.close(handle)
        self.path = Path(name)
        self._lock = threading.Lock()
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                capture.write(self.path, raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: object) -> None:  # silence access log
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def write(self, request_path: str, raw: bytes) -> None:
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode("utf-8", errors="replace")
        record = {
            "path": request_path,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "body": body,
        }
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def env(self) -> dict[str, str]:
        """Environment for the Claude Code CLI subprocess."""
        env = {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.endpoint,
            # Short intervals: the job is short-lived, so don't wait for the defaults.
            "OTEL_LOGS_EXPORT_INTERVAL": "1000",
            "OTEL_METRIC_EXPORT_INTERVAL": "5000",
        }
        if os.environ.get("OTEL_LOG_USER_PROMPTS", "").strip():
            env["OTEL_LOG_USER_PROMPTS"] = os.environ["OTEL_LOG_USER_PROMPTS"].strip()
        return env

    def close(self) -> None:
        time.sleep(2)  # let the CLI's final flush land
        self._server.shutdown()
        self._server.server_close()


def start_otel_capture() -> OtelCapture | None:
    if os.environ.get("OTEL_CAPTURE", "").strip() not in ("1", "true", "True"):
        return None
    capture = OtelCapture()
    log(f"[otel] capturing OTLP to {capture.path} via {capture.endpoint}")
    return capture


async def run_task(task: Task, cwd: Path, prompt: str, otel_env: dict[str, str] | None = None) -> str:
    """Drive the agent and return the report text."""
    options = ClaudeAgentOptions(
        env=otel_env or {},
        cwd=str(cwd),
        system_prompt=SYSTEM_PROMPT,
        setting_sources=setting_sources(),
        skills=[task.skill],
        allowed_tools=task.allowed_tools,
        # Deny anything not pre-approved instead of prompting: there is no TTY
        # here, and a permission prompt would hang the job until it times out.
        permission_mode="dontAsk",
        model=resolved_model(task),
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


def upload_blob_text(data: str | bytes, name: str, task_name: str, task: Task, stamp: str) -> None:
    """Upload `data` as {RUN_ID}/{MODEL}/{TASK}/{name}-{stamp}{ext}, when configured.

    Optional: a comparison run across models should still have its other
    results even if one upload fails, so this logs a warning and returns
    rather than raising — the report has already reached stdout/OUTPUT_DIR.
    """
    connection_string = os.environ.get("STORAGE_CONNECTION_STRING", "").strip()
    container = os.environ.get("BLOB_CONTAINER", "").strip()
    if not connection_string or not container:
        return

    run_id = os.environ.get("RUN_ID", "").strip() or stamp
    model = resolved_model(task)
    blob_path = f"{run_id}/{model}/{task_name}/{name.format(stamp=stamp)}"

    try:
        from azure.storage.blob import BlobServiceClient

        client = BlobServiceClient.from_connection_string(connection_string)
        client.get_blob_client(container, blob_path).upload_blob(data, overwrite=True)
        log(f"[output] uploaded to blob {container}/{blob_path}")
    except Exception as exc:  # noqa: BLE001 - never let an upload failure fail the job
        log(f"[warn] failed to upload {blob_path} to blob storage: {exc}")


def upload_report(report: str, task_name: str, task: Task, stamp: str) -> None:
    upload_blob_text(report, "report-{stamp}.md", task_name, task, stamp)


def upload_otel(capture: OtelCapture, task_name: str, task: Task) -> None:
    """Stop the receiver and upload what it captured. Never raises."""
    capture.close()
    if not capture.path.exists() or capture.path.stat().st_size == 0:
        log("[otel] nothing was captured")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log(f"[otel] captured {capture.path.stat().st_size} bytes")
    upload_blob_text(capture.path.read_bytes(), "otel-{stamp}.jsonl", task_name, task, stamp)


def publish(report: str, task_name: str, task: Task) -> None:
    """Emit the report to stdout, to OUTPUT_DIR when mounted, and to blob storage when configured."""
    print(report, flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    output_dir = os.environ.get("OUTPUT_DIR", "").strip()
    if output_dir:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("report.md", f"report-{stamp}.md"):
            (directory / name).write_text(report, encoding="utf-8")
        log(f"[output] wrote report to {directory}")

    upload_report(report, task_name, task, stamp)


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

    capture = start_otel_capture()
    try:
        report = await run_task(task, cwd, prompt, capture.env() if capture else None)
    finally:
        # Upload telemetry even when the run failed — that is when it matters most.
        if capture:
            upload_otel(capture, name, task)
    if not report:
        log("[error] the agent produced no report")
        return 1

    publish(report, name, task)
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
