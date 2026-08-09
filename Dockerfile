# Build for the architecture the job will run on — pass `--platform linux/amd64`
# to `docker build`. Azure Container Apps is amd64 unless you provision an Arm
# workload profile, and the claude-agent-sdk wheel bundles an architecture-
# specific binary, so a wheel for the wrong arch fails at runtime, not at build
# time. `az acr build` (see infra/deploy.sh) handles this for you.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: the skill diffs the target repo. ca-certificates: https clones + API calls.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent

WORKDIR /app

COPY requirements.txt ./
RUN pip install --requirement requirements.txt

# Skills are installed into the *user* scope so the agent finds them no matter
# which directory it is reviewing. See setting_sources() in run_agent.py.
COPY --chown=agent:agent .claude/skills/ /home/agent/.claude/skills/
COPY --chown=agent:agent app/ /app/app/

# The env-report skill runs this one script; the agent is granted permission to
# run this exact path and nothing else.
COPY --chown=agent:agent bin/ /app/bin/
RUN chmod +x /app/bin/collect-env.sh

# /workspace holds the cloned repo; /out is where a mounted volume can go.
RUN mkdir -p /workspace /out && chown agent:agent /workspace /out

# Belt-and-suspenders on ownership: COPY --chown sets the owner of the files it
# copies, but whether it also chowns newly-created *intermediate* directories
# (here, /home/agent/.claude, created fresh by the skills COPY above) varies
# by builder. When it doesn't, the Claude Code CLI's own state directory
# (~/.claude/, used for session bookkeeping) ends up root-owned and the CLI
# fails at startup with "Permission denied creating .../session-env". Force
# it explicitly rather than depend on COPY's behavior.
RUN chown -R agent:agent /home/agent

USER agent
ENV HOME=/home/agent \
    WORKSPACE_DIR=/workspace/repo

ENTRYPOINT ["python", "-m", "app.run_agent"]
