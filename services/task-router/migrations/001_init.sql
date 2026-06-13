-- migrations/001_init.sql
-- MVP SQLite-схема для task-router (ADR-003).
-- Идемпотентна: безопасно применять повторно (IF NOT EXISTS).
-- Версия: 001  Дата: 2026-06-10

-- ─────────────────────────────────────────────────────────────
-- triage_cache
-- KV-кэш результатов триажа (slow-path → fast-path повторных задач).
-- Ключ: sha256(нормализованный текст задачи).
-- Инвалидация по registry_ver: запись считается протухшей,
-- если registry_ver != текущей версии реестра.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS triage_cache (
    text_hash    TEXT    NOT NULL,          -- sha256 нормализованного текста
    tags         TEXT    NOT NULL,          -- JSON-массив канонов: ["go","grpc"]
    registry_ver TEXT    NOT NULL,          -- версия реестра на момент триажа
    updated_at   INTEGER NOT NULL,          -- unix-timestamp (секунды)
    PRIMARY KEY (text_hash)
);

-- ─────────────────────────────────────────────────────────────
-- routing_log
-- Append-only лог каждого решения маршрутизатора.
-- На MVP — SQLite; путь к ClickHouse описан в design.md ADR-003.
-- id: AUTOINCREMENT INTEGER (проще и компактнее uuid на SQLite).
-- Запись лога НЕ блокирует горячий путь (durable-слой развязан).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS routing_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,          -- unix-timestamp (секунды)
    text_hash    TEXT,                      -- sha256 нормализованного текста (nullable: pin_agent без текста)
    tags         TEXT,                      -- JSON-массив канонов: ["go","grpc"]
    path         TEXT    NOT NULL,          -- "fast" | "slow"
    candidates   TEXT,                      -- JSON [{agent,score,covered_tags}]
    chosen       TEXT,                      -- выбранный агент
    used_llm     INTEGER NOT NULL           -- 0 = детерминированный fast-path, 1 = после LLM-триажа
                 CHECK (used_llm IN (0, 1)),
    confidence   REAL,                      -- [0.0, 1.0]: доля покрытых тегов × нормированный score
    latency_us   INTEGER NOT NULL,          -- латентность матчинга, микросекунды
    registry_ver TEXT                       -- версия реестра на момент решения
);

-- Индекс по времени для агрегаций /metrics (доля fast_path за период, p99).
CREATE INDEX IF NOT EXISTS idx_routing_log_ts ON routing_log (ts);
