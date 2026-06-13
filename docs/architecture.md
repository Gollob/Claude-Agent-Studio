# Architecture

This document provides a more detailed view of the Claude Agent Studio architecture,
complementing the overview in the top-level [README](../README.md).

---

## System overview

Claude Agent Studio connects a personal Telegram chat to a team of Claude AI agents running
headlessly on a server. The system is composed of four independent Docker services:

| Service | Language | Port | Role |
|---|---|---|---|
| `bot` | Python | — (no host port) | Telegram long-poll dispatcher |
| `task-router` | Go | `127.0.0.1:8092` | Deterministic tag-based routing |
| `file-intake` | Python (FastAPI) | `127.0.0.1:8090` | AI-powered file/audio processor |
| `whisper` | Python (faster-whisper) | internal only | Speech-to-text |

---

## Bot (`bot/tgbridge`)

The bot is structured as a Python package (`tgbridge`) with these modules:

```
tgbridge/
  app.py          — top-level entrypoint; wires up Telegram long-poll loop
  config.py       — all env vars, IPC paths, mode registry, constants
  handlers.py     — per-message-type dispatch (text / voice / photo / document)
  workers.py      — async worker pool: one Claude subprocess per task
  state.py        — shared task state (FIFO queue, task registry, lock)
  process.py      — subprocess management (_kill_tree, graceful SIGTERM→SIGKILL)
  panel.py        — live status panel (single inline message, debounced edits)
  commands.py     — Telegram slash command handlers (/mode, /status, /queue, /cancel)
  tgapi.py        — thin wrapper around Telegram Bot API (long-poll, send, edit)
  media.py        — voice/photo/document forwarding to file-intake
  otel.py         — OpenTelemetry span helpers (no-op when not configured)
  secrets.py      — env-var masking for log output, child process env allowlist
```

### Mode routing

The bot maintains a sticky mode per chat. Mode determines which agent working directory
`claude -p` is launched in:

```python
MODES = {
    "ask": {"path": AGENT_WORKDIR,          "desc": "General assistant"},
    "dev": {"path": AGENT_WORKDIR + "/dev", "desc": "Dev studio"},
}
```

Within `dev` mode, specialist shortcuts (`/go`, `/py`, `/ts`, etc.) prepend a routing prefix
that the task-router resolves to a specific agent.

### Approval gate (ADR-003)

```
Claude agent issues Bash command
        |
        v
approval-hook.sh (PreToolUse hook)
        |
        v
classifier.py → allow / ask / deny
        |
   ask? |
        v
Write /tmp/agent-studio/approval.pending
Send Telegram inline buttons [Allow] [Deny]
Block (poll approval.decision, timeout 300 s)
        |
   User clicks button
        v
Bot writes approval.decision → hook unblocks
```

The classifier (`classifier.py`) is a pure, LLM-free function. It processes each pipeline
segment (`;`, `&&`, `|`) independently to avoid bypass through chaining.

---

## task-router (`services/task-router/`)

Internal Go package layout:

```
internal/
  registry/   — RCU in-memory index: tags.yaml → inverted index (tag → [agent])
  parser/     — parse request body → TaskEnvelope {slug, tags, text}
  matcher/    — RouteTarget selection: fast-path (slug exact match) then slow-path (scoring)
  dispatcher/ — HTTP client delivering the task payload to the chosen agent endpoint
  store/      — SQLite: routing_log table, WAL mode, pure-Go (no cgo)
  httpapi/    — HTTP server wiring all internal packages; OTel spans on /route
```

### Routing algorithm

```
POST /route  {text: "...", tags: ["go", "api"]}
        |
        v
Parser → TaskEnvelope
        |
        v
Matcher:
  1. Fast-path: does text contain project slug? → O(1) registry lookup → RouteTarget
  2. Slow-path: score each agent by sum of (tag_weight × skill_weight) for matching tags
               → highest score wins → RouteTarget
        |
        v
Response: {chosen: "go-dev", cwd: "/agents/dev", score: 2.4}
```

The registry reloads `tags.yaml` on service startup. To change routing rules, edit
`tagcatalog/tags.yaml` and restart the container.

### API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/route` | Route a task; returns `chosen` agent |
| `POST` | `/triage/callback` | Provide human-selected tags for slow-path re-route |
| `GET` | `/agents` | List registered agents from catalog |
| `GET` | `/health` | Health check |
| `GET` | `/metrics` | Prometheus-format counters (requests, latency, routing gaps) |

---

## file-intake (`services/file-intake/`)

```
POST /process  (multipart: file + metadata)
        |
        v
Content-type detection
  audio/* → Whisper STT → transcript text
  image/* → Claude vision API → description text
  application/pdf → text extraction → Claude summarization
        |
        v
Structured result (JSON)
        |
        v
Optional: persist to note-taking backend (ETAPI) if configured
Return result to bot → bot sends reply to Telegram
```

The Whisper STT service runs as a separate container (`faster-whisper`). It is health-checked
before file-intake starts (`depends_on: condition: service_healthy`). Model size and compute
type are configurable via `WHISPER_MODEL` / `WHISPER_COMPUTE` env vars.

---

## Agents (`agents/`)

Each agent is a Claude Code sub-agent definition: a Markdown file with YAML front-matter
consumed by the `claude` CLI.

```yaml
---
name: go-dev
description: >-
  Delegate high-throughput Go services: concurrency, low latency, gRPC/HTTP,
  database, observability, profiling. Not for frontend or Python.
tools: Read, Write, Edit, Bash, Grep, Glob
model: opus
---
```

The `description` field is also used by task-router's catalog (`tags.yaml`) to describe
the agent's zone. The `model` field determines which Claude model is invoked.

Agents run with `claude -p <prompt>` in their dedicated working directory. They have no
persistent memory between invocations unless the orchestrator passes context explicitly.

### Architect skill (OpenSpec)

The `agents/skills/architect/` skill implements a spec-driven design process:

```
New feature / system
        |
        v
architect agent
  1. proposal.md  — intent, scope, constraints
  2. specs/       — functional + non-functional requirements
  3. design.md    — component diagram, data flow, ADRs
  4. tasks.md     — atomic tasks tagged by responsible agent
        |
        v
Conductor delegates tasks to dev agents
```

The `board.sh.example` script shows how to integrate with a ETAPI-compatible task board.
It is an example only; the board integration is optional.

---

## Observability

All three services support OpenTelemetry tracing. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to
enable. When the endpoint is not set, all OTel calls degrade gracefully to no-ops — the
services start and run normally without a collector.

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://your-collector:4317
OTEL_SERVICE_NAMESPACE=agent-studio
OTEL_DEPLOYMENT_ENV=production
```

The task-router also exposes Prometheus-format counters at `GET /metrics`.

---

## Security considerations

See [SECURITY.md](../SECURITY.md) for the full security policy. Key points:

- Services bind to `127.0.0.1` — not exposed on public interfaces.
- All secrets are environment-variable only; `.env` is gitignored.
- The approval gate blocks dangerous commands before they execute.
- `TELEGRAM_TOKEN` is not forwarded to agent subprocesses.
