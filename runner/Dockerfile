# The scheduled ezup runner: "the existing CLI, scheduled, in a container the
# PM owns" (REMOTE-RUNNER-DESIGN section 3).
#
# Build from the REPOSITORY ROOT so the package sources are in context:
#     docker build -f runner/Dockerfile -t ezup-runner .
#
# The image deliberately does NOT contain the `claude` CLI: the runner drives
# the HTTP LLM provider (EZUP_LLM_*), so there is no `claude -p` and no
# interactive auth to carry.
FROM python:3.12-slim

# uv, copied from its official image -- the fast installer the project uses.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# System tools:
#   git    -- the pipeline's GIT corroboration stage shells out to it; it
#             degrades gracefully without repos present, but git must exist.
#   curl / unzip / ca-certificates -- only to install the AWS CLI below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (official installer), for S3-compatible upload to the PM's OWN
# bucket. uname -m yields x86_64 / aarch64, which match AWS's archive names,
# so the same Dockerfile builds on amd64 and arm64. This is a system tool, not
# a Python dependency of the package -- the package's one dep stays cryptography.
RUN arch="$(uname -m)" \
 && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${arch}.zip" -o /tmp/awscliv2.zip \
 && unzip -q /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscliv2.zip \
 && aws --version

WORKDIR /app

# Copy only what the package needs to install, so the image does not carry the
# repo's .venv/.git/tests. pyproject reads README.md, so it is required here.
COPY pyproject.toml README.md ./
COPY ezchangelog ./ezchangelog

# Install the package system-wide; this puts `ezup` and `ezcl` on PATH and
# pulls in cryptography (the only third-party dependency).
RUN uv pip install --system --no-cache .

# The runner scripts.
COPY runner/entrypoint.sh runner/upload.sh /app/runner/
RUN chmod +x /app/runner/entrypoint.sh /app/runner/upload.sh

# The trust-domain volume: decrypted transcripts and rendered journals live
# here. Encrypt it at rest with platform disk encryption.
ENV EZCHANGELOG_HOME=/data
VOLUME ["/data"]

# The runner drives the HTTP provider by default; the PM supplies the endpoint,
# key, and model ids at run time.
ENV EZUP_LLM_PROVIDER=http

ENTRYPOINT ["/app/runner/entrypoint.sh"]
