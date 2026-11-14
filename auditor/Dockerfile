# syntax=docker/dockerfile:1
# The council's workbench: one throwaway container per member per job (see auditors.py).
# Build once: python auditors.py build   (about 10 GB, mostly CUDA PyTorch)
FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev build-essential git curl ca-certificates tar \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv: fast venvs, and any Python version a project asks for
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Each member's hands: Gemini CLI, Claude Code (Claude, and DeepSeek, OpenRouter and local models drive it), Codex (GPT and Grok)
RUN npm install -g @google/gemini-cli @anthropic-ai/claude-code @openai/codex && npm cache clean --force

# A ready Python with CUDA PyTorch (cu128: needed for the RTX 50 series) and the usual science stack;
# a project's own venv can reuse it with --system-site-packages
# (patient with slow downloads, a few at a time; finished downloads are kept in a build cache, so a
# failed build picks up where it stopped instead of fetching 3 GB again)
ENV UV_HTTP_TIMEOUT=600 UV_CONCURRENT_DOWNLOADS=4 UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/base --python 3.12 \
    && uv pip install --python /opt/base torch --index-url https://download.pytorch.org/whl/cu128 \
    && uv pip install --python /opt/base numpy scipy pandas pytest biopython
ENV PATH=/opt/base/bin:$PATH VIRTUAL_ENV=/opt/base

# Not root: Claude Code refuses to skip its permission prompts as root, and nothing here needs it
RUN useradd -m auditor && mkdir -p /work /home/auditor/.cache && chown -R auditor /work /home/auditor
USER auditor
WORKDIR /work
