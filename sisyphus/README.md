# Sisyphus — Personal AI Agent Orchestration

Sisyphus orchestrates AI agents to automate software development workflows. It runs as a single Docker container, managing agents as subprocesses with git worktree isolation.

## Prerequisites

- **Docker** (with Compose V2)
- **opencode** binary (placed at `./bin/opencode` or in `$PATH`)

## Setup

1. **Clone the repository** (if not already done):
   ```bash
   git clone <repo-url> && cd sisyphus
   ```

2. **Configure secrets**:
   ```bash
   cp .env.example .env
   # Edit .env with your API keys and endpoints
   ```

3. **Start the system**:
   ```bash
   docker compose up --build
   ```

## Quick Start

```bash
# Build and start
docker compose up -d

# Attach to logs
docker compose logs -f

# Run a pipeline job
docker compose exec sisyphus python -m sisyphus run pipeline.yaml

# Run a single agent task
docker compose exec sisyphus python -m sisyphus run --agent explorer "research topic"

# Check scheduler status
docker compose exec sisyphus python -m sisyphus scheduler status

# Stop everything
docker compose down
```

## Configuration

All configuration lives in `config.yaml`. Environment variable substitution is supported with `${VAR_NAME}` syntax. Secrets go in `.env` (never committed).

## Project Structure

```
sisyphus/
├── core/           # Core engine — pipeline runner, agent lifecycle
├── agents/         # Agent implementations (subprocess wrappers)
├── tools/          # Tool implementations (git, docker, api clients)
├── workspace/      # Runtime data (git worktrees, logs, artifacts)
├── config.yaml     # System configuration
└── requirements.txt
```
