# Claude Agent Studio

A showcase/template architecture: Telegram bot conductor + team of Claude AI agents + microservices.

> Full README coming soon. This is the initial scaffold.

## Quick start

```bash
cp .env.example .env
# Edit .env: set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY
make up
```

## Components

- `bot/` — Telegram bridge bot (long-poll, mode routing, approval gate, classifier)
- `services/task-router/` — Go microservice for tag-based agent routing (:8092)
- `services/file-intake/` — FastAPI file/audio processor (:8090) + Whisper STT
- `agents/` — Claude agent definitions (9 specialists + architect skill)
- `infra/` — observability, CI config
- `.github/workflows/` — GitHub Actions CI

## License

Apache-2.0 — see [LICENSE](LICENSE).
