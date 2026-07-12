FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.144.1
ARG CLAUDE_CODE_VERSION=2.1.207
ARG NUMPY_VERSION=2.4.6
ARG SCIPY_VERSION=1.17.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential curl git python3 python3-pip ripgrep \
    && python3 -m pip install --break-system-packages --no-cache-dir \
        "numpy==${NUMPY_VERSION}" "scipy==${SCIPY_VERSION}" \
    && npm install -g \
        "@openai/codex@${CODEX_VERSION}" \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && rm -rf /var/lib/apt/lists/* /root/.npm

RUN useradd --create-home --uid 10001 worker
USER worker
WORKDIR /workspace
