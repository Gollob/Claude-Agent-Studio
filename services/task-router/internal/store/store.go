// Package store provides persistence for task routing history and audit log
// backed by SQLite (modernc.org/sqlite, pure-Go, no cgo). The schema is
// managed via migrations/001_init.sql, applied idempotently at startup.
//
// The durable layer is decoupled from the hot path: matching never touches
// the DB. Store is used only for triage_cache (KV upsert) and routing_log
// (append-only), plus aggregates for /metrics.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"time"

	_ "modernc.org/sqlite"
)

// Store wraps a SQLite database handle.
type Store struct {
	db *sql.DB
}

// Open opens (or creates) the SQLite database at dbPath and applies the
// migration found at migrationPath idempotently.
func Open(dbPath, migrationPath string) (*Store, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("store: open %s: %w", dbPath, err)
	}
	// SQLite is a single-writer engine; serialize writes to avoid "database is locked".
	db.SetMaxOpenConns(1)

	// Pragmas for durability/concurrency.
	for _, p := range []string{
		"PRAGMA journal_mode=WAL;",
		"PRAGMA busy_timeout=5000;",
		"PRAGMA foreign_keys=ON;",
	} {
		if _, err := db.Exec(p); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("store: pragma %q: %w", p, err)
		}
	}

	s := &Store{db: db}
	if err := s.applyMigration(migrationPath); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) applyMigration(path string) error {
	ddl, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("store: read migration %s: %w", path, err)
	}
	if _, err := s.db.Exec(string(ddl)); err != nil {
		return fmt.Errorf("store: apply migration: %w", err)
	}
	return nil
}

// Close closes the underlying database.
func (s *Store) Close() error { return s.db.Close() }

// Ping reports whether the database is reachable (used by /healthz).
func (s *Store) Ping(ctx context.Context) error { return s.db.PingContext(ctx) }

// ── triage_cache ──────────────────────────────────────────────────────────

// UpsertTriage stores (or replaces) the triage result for a normalized text hash.
func (s *Store) UpsertTriage(ctx context.Context, textHash string, tags []string, registryVer string) error {
	tagsJSON, err := json.Marshal(tags)
	if err != nil {
		return fmt.Errorf("store: marshal tags: %w", err)
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO triage_cache (text_hash, tags, registry_ver, updated_at)
		 VALUES (?, ?, ?, ?)
		 ON CONFLICT(text_hash) DO UPDATE SET
		   tags=excluded.tags,
		   registry_ver=excluded.registry_ver,
		   updated_at=excluded.updated_at`,
		textHash, string(tagsJSON), registryVer, time.Now().Unix())
	if err != nil {
		return fmt.Errorf("store: upsert triage: %w", err)
	}
	return nil
}

// TriageEntry is a cached triage result.
type TriageEntry struct {
	Tags        []string
	RegistryVer string
	UpdatedAt   int64
}

// GetTriage returns the cached triage for textHash. ok=false if absent.
func (s *Store) GetTriage(ctx context.Context, textHash string) (TriageEntry, bool, error) {
	var tagsJSON, ver string
	var updated int64
	row := s.db.QueryRowContext(ctx,
		`SELECT tags, registry_ver, updated_at FROM triage_cache WHERE text_hash = ?`, textHash)
	if err := row.Scan(&tagsJSON, &ver, &updated); err != nil {
		if err == sql.ErrNoRows {
			return TriageEntry{}, false, nil
		}
		return TriageEntry{}, false, fmt.Errorf("store: get triage: %w", err)
	}
	var tags []string
	if err := json.Unmarshal([]byte(tagsJSON), &tags); err != nil {
		return TriageEntry{}, false, fmt.Errorf("store: unmarshal triage tags: %w", err)
	}
	return TriageEntry{Tags: tags, RegistryVer: ver, UpdatedAt: updated}, true, nil
}

// ── routing_log ───────────────────────────────────────────────────────────

// RoutingEntry is one append-only routing decision record.
type RoutingEntry struct {
	TS          int64
	TextHash    string
	Tags        []string
	Path        string // "fast" | "slow"
	Candidates  any    // serialized as JSON
	Chosen      string
	UsedLLM     bool
	Confidence  float64
	LatencyUS   int64
	RegistryVer string
}

// AppendRoutingLog inserts one routing decision. Errors are returned but must
// not block the hot path (the caller logs and continues).
func (s *Store) AppendRoutingLog(ctx context.Context, e RoutingEntry) error {
	tagsJSON, err := json.Marshal(e.Tags)
	if err != nil {
		return fmt.Errorf("store: marshal tags: %w", err)
	}
	candJSON, err := json.Marshal(e.Candidates)
	if err != nil {
		return fmt.Errorf("store: marshal candidates: %w", err)
	}
	used := 0
	if e.UsedLLM {
		used = 1
	}
	ts := e.TS
	if ts == 0 {
		ts = time.Now().Unix()
	}
	var textHash any
	if e.TextHash != "" {
		textHash = e.TextHash
	}
	var chosen any
	if e.Chosen != "" {
		chosen = e.Chosen
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO routing_log
		   (ts, text_hash, tags, path, candidates, chosen, used_llm, confidence, latency_us, registry_ver)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		ts, textHash, string(tagsJSON), e.Path, string(candJSON), chosen, used,
		e.Confidence, e.LatencyUS, e.RegistryVer)
	if err != nil {
		return fmt.Errorf("store: append routing_log: %w", err)
	}
	return nil
}

// ── aggregates for /metrics ───────────────────────────────────────────────

// Metrics holds aggregated KPI values computed from routing_log.
type Metrics struct {
	Total          int64
	FastCount      int64
	SlowCount      int64
	UsedLLMCount   int64
	NoLLMCount     int64
	FastPathRatio  float64 // fast / total
	UsedLLMRatio   float64 // used_llm / total
	P99LatencyUS   int64
	P50LatencyUS   int64
	AvgConfidence  float64
}

// Aggregate computes KPI metrics from routing_log. Percentiles are computed in
// Go from the latency column (MVP volume is small; for scale use ClickHouse).
func (s *Store) Aggregate(ctx context.Context) (Metrics, error) {
	var m Metrics
	row := s.db.QueryRowContext(ctx, `
		SELECT
		  COUNT(*),
		  COALESCE(SUM(CASE WHEN path='fast' THEN 1 ELSE 0 END),0),
		  COALESCE(SUM(CASE WHEN path='slow' THEN 1 ELSE 0 END),0),
		  COALESCE(SUM(used_llm),0),
		  COALESCE(AVG(confidence),0)
		FROM routing_log`)
	if err := row.Scan(&m.Total, &m.FastCount, &m.SlowCount, &m.UsedLLMCount, &m.AvgConfidence); err != nil {
		return Metrics{}, fmt.Errorf("store: aggregate: %w", err)
	}
	m.NoLLMCount = m.Total - m.UsedLLMCount
	if m.Total > 0 {
		m.FastPathRatio = float64(m.FastCount) / float64(m.Total)
		m.UsedLLMRatio = float64(m.UsedLLMCount) / float64(m.Total)
	}

	// Percentiles via SQLite window (ordered scan). Small volume on MVP.
	lats, err := s.latencies(ctx)
	if err != nil {
		return Metrics{}, err
	}
	m.P50LatencyUS = percentile(lats, 0.50)
	m.P99LatencyUS = percentile(lats, 0.99)
	return m, nil
}

func (s *Store) latencies(ctx context.Context) ([]int64, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT latency_us FROM routing_log ORDER BY latency_us ASC`)
	if err != nil {
		return nil, fmt.Errorf("store: latencies: %w", err)
	}
	defer rows.Close()
	var out []int64
	for rows.Next() {
		var v int64
		if err := rows.Scan(&v); err != nil {
			return nil, fmt.Errorf("store: scan latency: %w", err)
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// percentile returns the p-th percentile (0..1) of a sorted ascending slice.
func percentile(sorted []int64, p float64) int64 {
	n := len(sorted)
	if n == 0 {
		return 0
	}
	if n == 1 {
		return sorted[0]
	}
	// nearest-rank
	idx := int(p * float64(n-1))
	if idx < 0 {
		idx = 0
	}
	if idx >= n {
		idx = n - 1
	}
	return sorted[idx]
}
