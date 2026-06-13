package store

import (
	"context"
	"path/filepath"
	"testing"
)

const migrationPath = "../../migrations/001_init.sql"

func openTest(t *testing.T) *Store {
	t.Helper()
	dir := t.TempDir()
	st, err := Open(filepath.Join(dir, "test.db"), migrationPath)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

// ── Ping ──────────────────────────────────────────────────────────────────

func TestPing(t *testing.T) {
	st := openTest(t)
	if err := st.Ping(context.Background()); err != nil {
		t.Errorf("Ping: %v", err)
	}
}

// ── triage_cache ──────────────────────────────────────────────────────────

func TestUpsertTriage_GetTriage(t *testing.T) {
	st := openTest(t)
	ctx := context.Background()

	hash := "abc123hash"
	tags := []string{"go", "grpc"}
	ver := "v1"

	if err := st.UpsertTriage(ctx, hash, tags, ver); err != nil {
		t.Fatalf("UpsertTriage: %v", err)
	}

	entry, ok, err := st.GetTriage(ctx, hash)
	if err != nil {
		t.Fatalf("GetTriage: %v", err)
	}
	if !ok {
		t.Fatal("expected entry, got ok=false")
	}
	if entry.RegistryVer != ver {
		t.Errorf("RegistryVer=%q want %q", entry.RegistryVer, ver)
	}
	if len(entry.Tags) != 2 || entry.Tags[0] != "go" || entry.Tags[1] != "grpc" {
		t.Errorf("Tags=%v want [go grpc]", entry.Tags)
	}
}

// TestUpsertTriage_OnlyOneRowAfterRepeat ensures ON CONFLICT upsert keeps a
// single row in triage_cache for the same text_hash (idempotent write).
func TestUpsertTriage_OnlyOneRowAfterRepeat(t *testing.T) {
	st := openTest(t)
	ctx := context.Background()

	hash := "same-hash"

	// Insert once.
	if err := st.UpsertTriage(ctx, hash, []string{"go"}, "v1"); err != nil {
		t.Fatalf("first upsert: %v", err)
	}
	// Update with new tags and version.
	if err := st.UpsertTriage(ctx, hash, []string{"clickhouse"}, "v2"); err != nil {
		t.Fatalf("second upsert: %v", err)
	}

	// Count rows directly — must be exactly 1.
	var count int
	row := st.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM triage_cache WHERE text_hash = ?", hash)
	if err := row.Scan(&count); err != nil {
		t.Fatalf("count: %v", err)
	}
	if count != 1 {
		t.Errorf("expected 1 row after double upsert, got %d", count)
	}

	// Latest value must reflect the second write.
	entry, ok, err := st.GetTriage(ctx, hash)
	if err != nil || !ok {
		t.Fatalf("GetTriage after upsert: ok=%v err=%v", ok, err)
	}
	if entry.RegistryVer != "v2" {
		t.Errorf("registry_ver=%q want v2", entry.RegistryVer)
	}
	if len(entry.Tags) != 1 || entry.Tags[0] != "clickhouse" {
		t.Errorf("Tags=%v want [clickhouse]", entry.Tags)
	}
}

func TestGetTriage_MissingReturnsNotFound(t *testing.T) {
	st := openTest(t)
	ctx := context.Background()
	_, ok, err := st.GetTriage(ctx, "nonexistent-hash")
	if err != nil {
		t.Fatalf("GetTriage for missing: %v", err)
	}
	if ok {
		t.Error("expected ok=false for absent hash")
	}
}

// ── routing_log ───────────────────────────────────────────────────────────

func TestAppendRoutingLog_And_Aggregate_Empty(t *testing.T) {
	st := openTest(t)
	ctx := context.Background()

	m, err := st.Aggregate(ctx)
	if err != nil {
		t.Fatalf("Aggregate on empty: %v", err)
	}
	if m.Total != 0 {
		t.Errorf("expected 0 total on empty DB, got %d", m.Total)
	}
	if m.FastPathRatio != 0 {
		t.Errorf("expected 0 fast_path_ratio on empty, got %f", m.FastPathRatio)
	}
}

func TestAppendRoutingLog_FastAndSlow(t *testing.T) {
	st := openTest(t)
	ctx := context.Background()

	fastEntries := []RoutingEntry{
		{Tags: []string{"go"}, Path: "fast", Chosen: "go-dev", UsedLLM: false, Confidence: 0.9, LatencyUS: 50, RegistryVer: "v1"},
		{Tags: []string{"grpc"}, Path: "fast", Chosen: "go-dev", UsedLLM: false, Confidence: 0.85, LatencyUS: 60, RegistryVer: "v1"},
		{Tags: []string{"clickhouse"}, Path: "fast", Chosen: "db-engineer", UsedLLM: false, Confidence: 0.8, LatencyUS: 55, RegistryVer: "v1"},
	}
	slowEntries := []RoutingEntry{
		{Tags: nil, Path: "slow", Chosen: "", UsedLLM: false, Confidence: 0, LatencyUS: 10, RegistryVer: "v1"},
		{Tags: []string{"go"}, Path: "fast", Chosen: "go-dev", UsedLLM: true, Confidence: 0.88, LatencyUS: 70, RegistryVer: "v1"},
	}

	for _, e := range fastEntries {
		if err := st.AppendRoutingLog(ctx, e); err != nil {
			t.Fatalf("AppendRoutingLog fast: %v", err)
		}
	}
	for _, e := range slowEntries {
		if err := st.AppendRoutingLog(ctx, e); err != nil {
			t.Fatalf("AppendRoutingLog slow: %v", err)
		}
	}

	m, err := st.Aggregate(ctx)
	if err != nil {
		t.Fatalf("Aggregate: %v", err)
	}
	if m.Total != 5 {
		t.Errorf("Total=%d want 5", m.Total)
	}
	if m.FastCount != 4 {
		t.Errorf("FastCount=%d want 4", m.FastCount)
	}
	if m.SlowCount != 1 {
		t.Errorf("SlowCount=%d want 1", m.SlowCount)
	}
	if m.UsedLLMCount != 1 {
		t.Errorf("UsedLLMCount=%d want 1", m.UsedLLMCount)
	}

	wantFPR := float64(4) / float64(5)
	if m.FastPathRatio < wantFPR-0.001 || m.FastPathRatio > wantFPR+0.001 {
		t.Errorf("FastPathRatio=%.4f want ~%.4f", m.FastPathRatio, wantFPR)
	}

	wantLLMR := float64(1) / float64(5)
	if m.UsedLLMRatio < wantLLMR-0.001 || m.UsedLLMRatio > wantLLMR+0.001 {
		t.Errorf("UsedLLMRatio=%.4f want ~%.4f", m.UsedLLMRatio, wantLLMR)
	}
}

func TestAppendRoutingLog_NullableFields(t *testing.T) {
	// Ensure zero-value TextHash and Chosen (empty strings) are stored as NULL
	// without violating constraints.
	st := openTest(t)
	ctx := context.Background()
	e := RoutingEntry{
		Tags:        nil,
		Path:        "slow",
		Chosen:      "",
		TextHash:    "",
		UsedLLM:     false,
		Confidence:  0,
		LatencyUS:   5,
		RegistryVer: "v1",
	}
	if err := st.AppendRoutingLog(ctx, e); err != nil {
		t.Fatalf("AppendRoutingLog with nulls: %v", err)
	}
	m, err := st.Aggregate(ctx)
	if err != nil {
		t.Fatalf("Aggregate: %v", err)
	}
	if m.Total != 1 {
		t.Errorf("Total=%d want 1", m.Total)
	}
}

// ── percentile (pure unit) ────────────────────────────────────────────────

func TestPercentile_Empty(t *testing.T) {
	if got := percentile(nil, 0.99); got != 0 {
		t.Errorf("percentile(nil, 0.99)=%d want 0", got)
	}
}

func TestPercentile_Single(t *testing.T) {
	if got := percentile([]int64{42}, 0.99); got != 42 {
		t.Errorf("percentile([42], 0.99)=%d want 42", got)
	}
}

func TestPercentile_Known(t *testing.T) {
	vals := []int64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
	// nearest-rank: p50 -> idx = int(0.5 * 9) = 4 -> sorted[4] = 5
	if got := percentile(vals, 0.5); got != 5 {
		t.Errorf("p50=%d want 5", got)
	}
	// p99 -> idx = int(0.99 * 9) = 8 -> sorted[8] = 9
	if got := percentile(vals, 0.99); got != 9 {
		t.Errorf("p99=%d want 9", got)
	}
}
