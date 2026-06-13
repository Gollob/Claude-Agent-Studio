# Contributing to Claude Agent Studio

Thank you for your interest in contributing. This document covers how to set up a local
development environment, run tests and linters, and submit a pull request.

---

## Prerequisites

- Docker and Docker Compose (for integration testing)
- Python 3.11+ and `pip` (bot and file-intake)
- Go 1.22+ (task-router)
- `make`

Optional but recommended:
- [`gitleaks`](https://github.com/gitleaks/gitleaks) for local secret scanning
- [`ruff`](https://docs.astral.sh/ruff/) for Python linting

---

## Local setup

```bash
git clone https://github.com/your-org/claude-agent-studio.git
cd claude-agent-studio

cp .env.example .env
chmod 600 .env
# Fill in at minimum: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY
# Use dummy/test values for unit-test runs (no live bot needed)
```

### Bot (Python)

```bash
cd bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # if present, otherwise stdlib-only
pip install pytest ruff
```

### task-router (Go)

```bash
cd services/task-router
go mod download
```

### file-intake (Python / FastAPI)

```bash
cd services/file-intake/app
pip install -r requirements.txt
```

---

## Running tests

```bash
# All tests at once (from repo root)
make test

# Bot only
cd bot && python -m pytest tests/ -v

# task-router only
cd services/task-router && go test ./...
```

The bot test suite uses `pytest` with `monkeypatch` for all external calls — no live Telegram
or Claude API is required. The task-router tests are standard `go test` table-driven tests.

---

## Linters

```bash
# All linters
make lint

# Python (ruff)
cd bot && python -m ruff check .

# Go
cd services/task-router && go vet ./...
```

The CI pipeline runs both. PRs with lint errors will not be merged.

---

## Secret scan

Before opening a PR, run a secret scan locally:

```bash
make scan
```

This uses `gitleaks` (installed locally or pulled as a Docker image). The scan checks for
API keys, tokens, and other credential patterns. **No PR will be accepted that introduces
secrets or credentials into the codebase.**

Rules:
- All secrets must be supplied via environment variables (see `.env.example`).
- `.env` files are in `.gitignore` — never commit them.
- Do not hardcode tokens, IDs, or passwords in source code or test fixtures.

---

## Code style

**Python:**
- Format with `ruff format` (or `black`). Line length 100.
- Type hints on all public functions.
- Docstrings for modules and non-trivial functions.

**Go:**
- `gofmt` formatting (enforced in CI via `gofmt -l`).
- `go vet` must pass with no warnings.
- Table-driven tests, `go test -race` clean.
- Structured logging via `slog`.

**Markdown / YAML:**
- Agent definition files (`agents/*.md`) use YAML front-matter with `name`, `description`,
  `tools`, `model` fields. Keep descriptions concise and accurate.
- `tagcatalog/tags.yaml` changes must be validated:
  ```bash
  cd services/task-router/tagcatalog && python validate.py
  ```

---

## Pull request process

1. Fork the repository and create a feature branch from `main`.
2. Make your changes with clear, atomic commits.
3. Ensure `make test`, `make lint`, and `make scan` all pass locally.
4. Open a PR against `main`. Describe:
   - What the change does and why.
   - How you tested it.
   - Any breaking changes or migration steps.
5. At least one maintainer review is required before merge.
6. Squash-merge is preferred for feature branches; fix-up commits are acceptable for patches.

---

## Adding a new agent

1. Create `agents/<name>.md` with YAML front-matter:
   ```yaml
   ---
   name: <name>
   description: <one-line description for the task-router>
   tools: Read, Write, Edit, Bash, Grep, Glob
   model: sonnet   # or opus for heavy reasoning tasks
   ---
   ```
2. Add the agent to `services/task-router/tagcatalog/tags.yaml` under `agents:` with
   appropriate `tags` entries under `skills:`.
3. Validate the catalog: `cd services/task-router/tagcatalog && python validate.py`.
4. Document the agent's scope in this PR description.

---

## Reporting bugs

Open a GitHub issue with:
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs (redact any tokens or personal information)

For security vulnerabilities, see [SECURITY.md](SECURITY.md).
