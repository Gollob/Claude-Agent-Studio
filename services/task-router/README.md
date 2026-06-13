# task-router

Детерминированный роутер задач для Dev Studio (agent-vm).

## Назначение

Сервис принимает входящие задачи (от Telegram-бота, cron, webhook) и детерминированно
направляет их нужному субагенту (agent-id) по правилам из каталога тегов (`tagcatalog/tags.yaml`).

**Ключевые свойства:**
- Fast-path: прямое совпадение по project slug — O(1) по in-memory индексу.
- Slow-path: scoring по тегам/ключевым словам — линейный проход по правилам.
- Персистентность: SQLite (pure-Go, без cgo), только для audit log и истории маршрутов.
- Transport: HTTP-only, слушает на `127.0.0.1:8092` (не выставляется наружу).

## Спека

`~/agent/dev/openspec/changes/add-task-router/`

## Структура каталогов

```
cmd/task-router/   — точка входа (main.go)
internal/
  registry/        — in-memory индекс правил из tags.yaml
  parser/          — разбор входящего payload → TaskEnvelope
  matcher/         — выбор RouteTarget по правилам (fast/slow path)
  dispatcher/      — HTTP-доставка задачи целевому агенту
  store/           — SQLite: audit log и история маршрутов
  httpapi/         — HTTP API (POST /route, GET /health, GET /metrics)
tagcatalog/        — tags.yaml + схема (заполняет db-engineer)
migrations/        — SQLite DDL-миграции (нумерованные)
deploy/            — Docker Compose (заполняет devops, Wave 4)
tests/             — интеграционные и unit-тесты
```

## Сборка

```bash
go build ./...
go vet ./...
# Бинарь:
go build -o task-router ./cmd/task-router/
./task-router
```

## Запуск (заглушка)

```bash
cp .env.example .env
# отредактируй .env под своё окружение
./task-router
```

Реальный запуск сервера реализует go-dev (Wave 2 наряда add-task-router).
