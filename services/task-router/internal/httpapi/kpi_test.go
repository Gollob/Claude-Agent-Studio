package httpapi

// KPI / token-economy end-to-end tests.
//
// ADR-005 guarantees the service NEVER calls an LLM: used_llm=true is only set
// when the orchestrator calls /triage/callback (the LLM ran externally).
// These tests validate:
//  1. fast_path_ratio reported by /metrics matches the actual mix.
//  2. used_llm_ratio=0 for purely fast-path traffic (no LLM involvement).
//  3. After /triage/callback the same text is served fast-path from triage cache.

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"task-router/internal/registry"
	"task-router/internal/store"
)

// kpiYAML has 3 known tags so we can create "fast" tasks (tagged) and
// "slow" tasks (no known tags) in a controlled ratio.
const kpiYAML = `
tags:
  - canon: go
    aliases: [golang]
    domain: golang
    weight: 1.2
  - canon: grpc
    aliases: [proto]
    domain: golang
    weight: 1.1
  - canon: clickhouse
    aliases: [ch]
    domain: db
    weight: 1.2
agents:
  - agent: go-dev
    model: opus
    heavy: 1
  - agent: db-engineer
    model: sonnet
    heavy: 0
skills:
  - skill: golang-grpc
    agent: go-dev
    tags: [go, grpc]
    weight: 1.8
  - skill: ch
    agent: db-engineer
    tags: [clickhouse]
    weight: 1.5
`

// newTestServerWithYAML builds a Server wired to a temp SQLite DB, using the
// given YAML bytes for the registry. Mirrors newTestServer but accepts YAML.
func newTestServerWithYAML(t *testing.T, yaml string) *Server {
	t.Helper()
	dir := t.TempDir()
	tagsPath := filepath.Join(dir, "tags.yaml")
	if err := os.WriteFile(tagsPath, []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	reg, err := registry.LoadFromFile(tagsPath)
	if err != nil {
		t.Fatalf("LoadFromFile: %v", err)
	}
	var ptr atomic.Pointer[registry.Registry]
	ptr.Store(reg)

	dbPath := filepath.Join(dir, "test.db")
	st, err := store.Open(dbPath, "../../migrations/001_init.sql")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })

	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	srv := New(&ptr, st, tagsPath, log)
	// Drain the log worker before the store is closed (LIFO cleanup order).
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	})
	return srv
}

// kpiServer returns a server built from kpiYAML via newTestServerFromYAML (inline).
func kpiServer(t *testing.T) *Server {
	t.Helper()
	return newTestServerWithYAML(t, kpiYAML)
}

// TestKPI_FastPathRatio verifies /metrics returns correct fast_path_ratio
// after a known mix of fast (tagged) and slow (untagged) tasks.
func TestKPI_FastPathRatio(t *testing.T) {
	srv := kpiServer(t)

	// Route 4 fast tasks (#go, #grpc, #clickhouse, alias golang).
	fastTasks := []string{
		`{"text":"#go build the service","task_id":"f1"}`,
		`{"text":"#grpc protocol buffer","task_id":"f2"}`,
		`{"text":"#clickhouse ingest","task_id":"f3"}`,
		`{"text":"#golang setup","task_id":"f4"}`,
	}
	// Route 2 slow tasks (no known tags).
	slowTasks := []string{
		`{"text":"please do vague stuff","task_id":"s1"}`,
		`{"text":"нет тегов здесь вообще","task_id":"s2"}`,
	}

	for _, body := range fastTasks {
		resp, m := do(t, srv, "POST", "/route", body)
		if resp.StatusCode != 200 {
			t.Fatalf("fast task status=%d", resp.StatusCode)
		}
		if m["path"] != "fast" {
			t.Errorf("task %q: path=%v want fast", body, m["path"])
		}
		if m["used_llm"] != false {
			t.Errorf("task %q: used_llm=%v want false", body, m["used_llm"])
		}
	}
	for _, body := range slowTasks {
		resp, m := do(t, srv, "POST", "/route", body)
		if resp.StatusCode != 200 {
			t.Fatalf("slow task status=%d", resp.StatusCode)
		}
		if m["path"] != "slow" {
			t.Errorf("task %q: path=%v want slow", body, m["path"])
		}
		if m["used_llm"] != false {
			t.Errorf("slow task %q: used_llm=%v want false (service never calls LLM)", body, m["used_llm"])
		}
	}

	// Read /metrics.
	req2, body2 := do(t, srv, "GET", "/metrics", "")
	if req2.StatusCode != 200 {
		t.Fatalf("/metrics status=%d", req2.StatusCode)
	}
	_ = body2 // /metrics returns text/plain; parse the body via raw response
	rawMetrics := readPlainMetrics(t, srv)

	fastPathRatio := parseMetricFloat(t, rawMetrics, "task_router_fast_path_ratio")
	// 4 fast + 2 slow = 6 total; fast_path_ratio = 4/6 ≈ 0.666667
	const wantRatio = 4.0 / 6.0
	if fastPathRatio < wantRatio-0.01 || fastPathRatio > wantRatio+0.01 {
		t.Errorf("fast_path_ratio=%.4f want ~%.4f (4/6)", fastPathRatio, wantRatio)
	}

	usedLLMRatio := parseMetricFloat(t, rawMetrics, "task_router_used_llm_ratio")
	if usedLLMRatio != 0.0 {
		t.Errorf("used_llm_ratio=%.4f want 0 (service never calls LLM)", usedLLMRatio)
	}
}

// TestKPI_TriageCachePromotesToFastPath verifies the full slow→callback→fast flow:
// after /triage/callback the same text is resolved via the triage cache
// (fast-path, from_cache=true) without any LLM call by the service itself.
func TestKPI_TriageCachePromotesToFastPath(t *testing.T) {
	srv := kpiServer(t)
	text := "ingest analytics events into the warehouse"

	// 1. First route: no known tags → slow.
	_, m1 := do(t, srv, "POST", "/route",
		fmt.Sprintf(`{"text":%q,"task_id":"kpi-t1"}`, text))
	if m1["path"] != "slow" {
		t.Fatalf("step1: path=%v want slow", m1["path"])
	}
	if m1["used_llm"] != false {
		t.Errorf("step1: used_llm=%v want false", m1["used_llm"])
	}

	// 2. Orchestrator posts /triage/callback with tags resolved by external LLM.
	_, m2 := do(t, srv, "POST", "/triage/callback",
		fmt.Sprintf(`{"task_id":"kpi-t1","text":%q,"tags":["clickhouse"]}`, text))
	if m2["chosen"] != "db-engineer" {
		t.Errorf("step2 callback chosen=%v want db-engineer", m2["chosen"])
	}
	// used_llm=true in callback response: the orchestrator flagged it as LLM-assisted.
	if m2["used_llm"] != true {
		t.Errorf("step2: used_llm=%v want true", m2["used_llm"])
	}
	if m2["cached"] != true {
		t.Errorf("step2: cached=%v want true (text provided → should cache)", m2["cached"])
	}

	// 3. Same text again: must be served fast-path from triage cache.
	_, m3 := do(t, srv, "POST", "/route",
		fmt.Sprintf(`{"text":%q,"task_id":"kpi-t1b"}`, text))
	if m3["path"] != "fast" {
		t.Errorf("step3: path=%v want fast (cache hit)", m3["path"])
	}
	if m3["from_cache"] != true {
		t.Errorf("step3: from_cache=%v want true", m3["from_cache"])
	}
	if m3["chosen"] != "db-engineer" {
		t.Errorf("step3: chosen=%v want db-engineer", m3["chosen"])
	}
	// Service itself never calls LLM on cached fast-path.
	if m3["used_llm"] != false {
		t.Errorf("step3: used_llm=%v want false (cache hit, no LLM by service)", m3["used_llm"])
	}

	// 4. Confirm /metrics: fast_path_ratio reflects the cache promotion.
	//    Traffic: 1 slow + 1 fast-from-callback + 1 fast-from-cache = 3 total,
	//    fast_count = 2 (callback logs "fast" + route-from-cache logs "fast").
	rawMetrics := readPlainMetrics(t, srv)
	fastPathRatio := parseMetricFloat(t, rawMetrics, "task_router_fast_path_ratio")
	// 2 fast out of 3 total ≈ 0.666
	if fastPathRatio < 0.60 {
		t.Errorf("after cache promotion fast_path_ratio=%.3f want >= 0.60", fastPathRatio)
	}
}

// TestKPI_NoLLMOnPureFastTraffic confirms that a batch of purely fast-path tasks
// results in used_llm_ratio=0 in /metrics.
func TestKPI_NoLLMOnPureFastTraffic(t *testing.T) {
	srv := kpiServer(t)

	for i := 0; i < 5; i++ {
		body := fmt.Sprintf(`{"text":"#go task number %d","task_id":"ll%d"}`, i, i)
		_, m := do(t, srv, "POST", "/route", body)
		if m["path"] != "fast" {
			t.Errorf("task %d path=%v want fast", i, m["path"])
		}
	}

	rawMetrics := readPlainMetrics(t, srv)
	usedLLMRatio := parseMetricFloat(t, rawMetrics, "task_router_used_llm_ratio")
	if usedLLMRatio != 0.0 {
		t.Errorf("used_llm_ratio=%.4f want exactly 0 on pure fast-path traffic", usedLLMRatio)
	}
	usedLLMTotal := parseMetricFloat(t, rawMetrics, "task_router_used_llm_total")
	if usedLLMTotal != 0.0 {
		t.Errorf("used_llm_total=%.0f want 0", usedLLMTotal)
	}
}

// ── helpers ───────────────────────────────────────────────────────────────

// readPlainMetrics returns the raw text/plain body of GET /metrics.
func readPlainMetrics(t *testing.T, srv *Server) string {
	t.Helper()
	req := httptest.NewRequest("GET", "/metrics", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("/metrics status=%d", rec.Code)
	}
	return rec.Body.String()
}

// parseMetricFloat extracts the float value of a Prometheus-style gauge/counter
// line: "<name> <value>" or "<name>{...} <value>".
func parseMetricFloat(t *testing.T, body, name string) float64 {
	t.Helper()
	for _, line := range strings.Split(body, "\n") {
		if strings.HasPrefix(line, "#") || strings.TrimSpace(line) == "" {
			continue
		}
		// match lines that start with name followed by space or '{'
		if !strings.HasPrefix(line, name+" ") && !strings.HasPrefix(line, name+"{") {
			continue
		}
		// value is the last token
		parts := strings.Fields(line)
		if len(parts) < 2 {
			continue
		}
		var v float64
		_, err := fmt.Sscanf(parts[len(parts)-1], "%f", &v)
		if err != nil {
			t.Fatalf("parseMetricFloat(%q): sscanf %q: %v", name, parts[len(parts)-1], err)
		}
		return v
	}
	t.Fatalf("metric %q not found in:\n%s", name, body)
	return 0
}
