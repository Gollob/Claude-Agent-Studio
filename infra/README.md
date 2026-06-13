# infra/

Optional infrastructure components. Not required to run the core bot or task-router.

## ClickHouse (optional — analytics / audit log)

A self-hosted [ClickHouse](https://clickhouse.com/) instance can replace or supplement
the default SQLite audit store in task-router.

Bring it up with the official Docker image:

```yaml
# Example docker-compose snippet
services:
  clickhouse:
    image: clickhouse/clickhouse-server:25-alpine
    restart: unless-stopped
    ports:
      - "127.0.0.1:8123:8123"   # HTTP
      - "127.0.0.1:9000:9000"   # Native
    volumes:
      - clickhouse_data:/var/lib/clickhouse
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8123/ping"]
      interval: 10s
      retries: 5

volumes:
  clickhouse_data:
```

Set `CLICKHOUSE_DSN` in your `.env` and enable the ClickHouse store in `task-router`
(see `services/task-router/.env.example`).

## Uptrace (optional — distributed tracing)

[Uptrace](https://uptrace.dev/) is a self-hosted OpenTelemetry backend.
Set `OTEL_EXPORTER_OTLP_ENDPOINT` and `UPTRACE_PROJECT_TOKEN` to enable tracing.
