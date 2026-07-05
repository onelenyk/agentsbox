# =============================================================================
# Sisyphus — Personal AI Agent Orchestration
# =============================================================================
#
# Prerequisites (external):
#   - opencode binary must be available in PATH or mounted via volume at
#     runtime. It is intentionally NOT installed in this image because it
#     is an external binary that runs on the host or is injected separately.
#
# Build:
#   docker build -t sisyphus .
#
# Run:
#   docker run --rm -v ./workspace:/workspace --env-file .env sisyphus
# =============================================================================

FROM python:3.12-slim

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------
# git      — for git worktree isolation and repo operations
# curl     — for API calls and health checks
# ca-certificates — for TLS verification
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime workspace (bind-mount at runtime)
# ---------------------------------------------------------------------------
WORKDIR /app
RUN mkdir -p /workspace

# ---------------------------------------------------------------------------
# Application source
# ---------------------------------------------------------------------------
COPY . .

# ---------------------------------------------------------------------------
# Install opencode (Linux ARM64)
# ---------------------------------------------------------------------------
RUN curl -sL "https://github.com/opencode-ai/opencode/releases/download/v0.0.55/opencode-linux-arm64.tar.gz" | tar xz -C /tmp \
    && mv /tmp/opencode /usr/local/bin/opencode \
    && chmod +x /usr/local/bin/opencode

# ---------------------------------------------------------------------------
# Health check — ensures git (always available) and optionally opencode
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD which opencode || which git

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
# NOTE: opencode must be accessible at runtime (mounted or in PATH).
ENTRYPOINT ["python", "/app/sisyphus.py"]
