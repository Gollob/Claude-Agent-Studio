package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"task-router/internal/parser"
	"task-router/internal/registry"
	"task-router/internal/store"
)

const apiYAML = `
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

func newTestServer(t *testing.T) (*Server, string) {
	t.Helper()
	dir := t.TempDir()
	tagsPath := filepath.Join(dir, "tags.yaml")
	if err := os.WriteFile(tagsPath, []byte(apiYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	reg, err := registry.LoadFromFile(tagsPath)
	if err != nil {
		t.Fatal(err)
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
	// Drain the log worker before the store is closed (cleanup runs LIFO, so
	// this is registered after the store-close cleanup and thus runs first).
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	})
	return srv, tagsPath
}

func do(t *testing.T, srv *Server, method, path, body string) (*http.Response, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	resp := rec.Result()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	if len(raw) > 0 && raw[0] == '{' {
		_ = json.Unmarshal(raw, &m)
	}
	return resp, m
}

func TestRoute_FastPath(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, m := do(t, srv, "POST", "/route", `{"text":"#go #grpc build gateway"}`)
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	if m["path"] != "fast" {
		t.Errorf("path=%v want fast", m["path"])
	}
	if m["chosen"] != "go-dev" {
		t.Errorf("chosen=%v want go-dev", m["chosen"])
	}
	if m["used_llm"] != false {
		t.Errorf("used_llm=%v want false", m["used_llm"])
	}
}

func TestRoute_SlowPath(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, m := do(t, srv, "POST", "/route", `{"text":"please do something vague","task_id":"x1"}`)
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	if m["path"] != "slow" {
		t.Errorf("path=%v want slow", m["path"])
	}
	if m["status"] != "needs_triage" {
		t.Errorf("status=%v want needs_triage", m["status"])
	}
	if _, ok := m["handoff"]; !ok {
		t.Errorf("missing handoff contract")
	}
	if m["used_llm"] != false {
		t.Errorf("used_llm must be false (service never calls LLM)")
	}
}

func TestTriageCallback_ThenCached(t *testing.T) {
	srv, _ := newTestServer(t)
	text := "ingest analytics events into the warehouse"

	// 1. First route => slow.
	_, m1 := do(t, srv, "POST", "/route", `{"text":"`+text+`","task_id":"t1"}`)
	if m1["path"] != "slow" {
		t.Fatalf("expected slow first, got %v", m1["path"])
	}

	// 2. Orchestrator returns tags via callback.
	_, m2 := do(t, srv, "POST", "/triage/callback",
		`{"task_id":"t1","text":"`+text+`","tags":["clickhouse"]}`)
	if m2["chosen"] != "db-engineer" {
		t.Errorf("callback chosen=%v want db-engineer", m2["chosen"])
	}
	if m2["used_llm"] != true {
		t.Errorf("callback used_llm should be true")
	}

	// 3. Same task again => now fast from cache.
	_, m3 := do(t, srv, "POST", "/route", `{"text":"`+text+`","task_id":"t1b"}`)
	if m3["path"] != "fast" {
		t.Errorf("expected fast from cache, got %v", m3["path"])
	}
	if m3["from_cache"] != true {
		t.Errorf("from_cache=%v want true", m3["from_cache"])
	}
	if m3["chosen"] != "db-engineer" {
		t.Errorf("cached chosen=%v want db-engineer", m3["chosen"])
	}
}

func TestTriageCallback_TextHash_ThenCached(t *testing.T) {
	srv, _ := newTestServer(t)
	text := "load batched events into the columnar store"

	// 1. First route => slow. Capture the text_hash the service returns; this is
	//    exactly what the orchestrator echoes back (it never sees the raw text on
	//    the callback contract).
	_, m1 := do(t, srv, "POST", "/route", `{"text":"`+text+`"}`)
	if m1["path"] != "slow" {
		t.Fatalf("expected slow first, got %v", m1["path"])
	}
	textHash, _ := m1["text_hash"].(string)
	if textHash == "" {
		t.Fatalf("slow response missing text_hash")
	}

	// 2. Callback with text_hash only (no text) + valid tags => 200, cached:true.
	resp2, m2 := do(t, srv, "POST", "/triage/callback",
		`{"text_hash":"`+textHash+`","tags":["clickhouse"]}`)
	if resp2.StatusCode != 200 {
		t.Fatalf("callback status=%d body=%v", resp2.StatusCode, m2)
	}
	if m2["cached"] != true {
		t.Errorf("cached=%v want true", m2["cached"])
	}
	if m2["chosen"] != "db-engineer" {
		t.Errorf("callback chosen=%v want db-engineer", m2["chosen"])
	}

	// 3. Same text again => fast from cache with the cached canon tags.
	_, m3 := do(t, srv, "POST", "/route", `{"text":"`+text+`"}`)
	if m3["path"] != "fast" {
		t.Errorf("expected fast from cache, got %v", m3["path"])
	}
	if m3["from_cache"] != true {
		t.Errorf("from_cache=%v want true", m3["from_cache"])
	}
	if m3["chosen"] != "db-engineer" {
		t.Errorf("cached chosen=%v want db-engineer", m3["chosen"])
	}
}

func TestTriageCallback_NoKey_NotCached(t *testing.T) {
	srv, _ := newTestServer(t)
	// Neither text nor text_hash => cannot key the cache; tags still resolve but
	// nothing is cached, so cached:false.
	resp, m := do(t, srv, "POST", "/triage/callback", `{"tags":["clickhouse"]}`)
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d body=%v", resp.StatusCode, m)
	}
	if m["cached"] != false {
		t.Errorf("cached=%v want false (no text/text_hash key)", m["cached"])
	}
	if m["chosen"] != "db-engineer" {
		t.Errorf("chosen=%v want db-engineer", m["chosen"])
	}
}

func TestTriageCallback_EmptyTags_422(t *testing.T) {
	srv, _ := newTestServer(t)
	// Valid 64-char sha256 hex so the hash check passes and we reach the
	// no-canonical-tags branch (422), not the format check (400).
	validHash := strings.Repeat("a", 64)
	resp, _ := do(t, srv, "POST", "/triage/callback",
		`{"text_hash":"`+validHash+`","tags":["totally-unknown-tag"]}`)
	if resp.StatusCode != 422 {
		t.Errorf("status=%d want 422 (no canonical tags)", resp.StatusCode)
	}
}

// TestTriageCallback_KeyDedup proves that closing the loop via the full text and
// via the text_hash from /route write to the SAME triage_cache row (one key), not
// two. The hash the orchestrator echoes must equal HashText(text).
func TestTriageCallback_KeyDedup(t *testing.T) {
	srv, _ := newTestServer(t)
	text := "stream rollup events into the columnar warehouse"

	// /route slow-path returns the text_hash the orchestrator would echo back.
	_, m1 := do(t, srv, "POST", "/route", `{"text":"`+text+`"}`)
	if m1["path"] != "slow" {
		t.Fatalf("expected slow first, got %v", m1["path"])
	}
	routeHash, _ := m1["text_hash"].(string)
	if routeHash == "" {
		t.Fatalf("slow response missing text_hash")
	}
	// The hash returned by /route must match what HashText(text) yields locally.
	if got := parser.HashText(text); got != routeHash {
		t.Fatalf("HashText(text)=%q != /route text_hash=%q", got, routeHash)
	}

	// Path A: callback with the full text (service hashes it itself).
	_, ma := do(t, srv, "POST", "/triage/callback",
		`{"text":"`+text+`","tags":["clickhouse"]}`)
	if ma["cached"] != true {
		t.Fatalf("text-path callback cached=%v want true", ma["cached"])
	}

	// Path B: callback with the text_hash echoed from /route.
	_, mb := do(t, srv, "POST", "/triage/callback",
		`{"text_hash":"`+routeHash+`","tags":["clickhouse"]}`)
	if mb["cached"] != true {
		t.Fatalf("hash-path callback cached=%v want true", mb["cached"])
	}

	// Both paths must resolve to the SAME single cache row under routeHash.
	srv.flushLog()
	entry, ok, err := srv.store.GetTriage(context.Background(), routeHash)
	if err != nil {
		t.Fatalf("GetTriage: %v", err)
	}
	if !ok {
		t.Fatalf("no triage_cache row under text_hash %q", routeHash)
	}
	if len(entry.Tags) != 1 || entry.Tags[0] != "clickhouse" {
		t.Errorf("cached tags=%v want [clickhouse]", entry.Tags)
	}
}

// TestTriageCallback_BadHash_400 rejects malformed text_hash before it can poison
// the cache under a forged key.
func TestTriageCallback_BadHash_400(t *testing.T) {
	srv, _ := newTestServer(t)
	cases := map[string]string{
		"too short":     "abc",
		"64 non-hex":    strings.Repeat("g", 64), // 64 chars but 'g' is not hex
		"uppercase hex": strings.Repeat("A", 64), // HashText emits lowercase only
		"wrong length":  strings.Repeat("a", 63), // 63 hex chars
	}
	for name, h := range cases {
		t.Run(name, func(t *testing.T) {
			resp, m := do(t, srv, "POST", "/triage/callback",
				`{"text_hash":"`+h+`","tags":["clickhouse"]}`)
			if resp.StatusCode != 400 {
				t.Errorf("status=%d want 400 (body=%v)", resp.StatusCode, m)
			}
		})
	}
}

func TestDispatch(t *testing.T) {
	srv, _ := newTestServer(t)
	body := `{"tasks":[
		{"task_id":"a","text":"#go x"},
		{"task_id":"b","text":"#clickhouse y"},
		{"task_id":"c","text":"vague text no tags"}
	],"max_parallel":2}`
	resp, _ := do(t, srv, "POST", "/dispatch", body)
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	raw := decodeFull(t, srv, body)
	if len(raw.NeedsTriage) != 1 {
		t.Errorf("needs_triage=%d want 1", len(raw.NeedsTriage))
	}
	if len(raw.Waves) == 0 {
		t.Errorf("expected waves")
	}
}

type dispatchResp struct {
	Waves       [][]map[string]any `json:"waves"`
	NeedsTriage []map[string]any   `json:"needs_triage"`
}

func decodeFull(t *testing.T, srv *Server, body string) dispatchResp {
	req := httptest.NewRequest("POST", "/dispatch", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	var dr dispatchResp
	if err := json.NewDecoder(rec.Body).Decode(&dr); err != nil {
		t.Fatal(err)
	}
	return dr
}

func TestHealthz(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, m := do(t, srv, "GET", "/healthz", "")
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	if m["status"] != "ok" {
		t.Errorf("status=%v", m["status"])
	}
	if m["registry_version"] == "" || m["registry_version"] == nil {
		t.Errorf("missing registry_version")
	}
	if m["db_status"] != "ok" {
		t.Errorf("db_status=%v want ok", m["db_status"])
	}
}

func TestMetrics(t *testing.T) {
	srv, _ := newTestServer(t)
	// generate some traffic
	do(t, srv, "POST", "/route", `{"text":"#go build"}`)
	do(t, srv, "POST", "/route", `{"text":"vague"}`)

	req := httptest.NewRequest("GET", "/metrics", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	body := rec.Body.String()
	for _, want := range []string{"task_router_fast_path_ratio", "task_router_used_llm_total", "task_router_route_latency_us"} {
		if !strings.Contains(body, want) {
			t.Errorf("metrics missing %q", want)
		}
	}
}

func TestReadViews(t *testing.T) {
	srv, _ := newTestServer(t)
	for _, p := range []string{"/agents", "/tags", "/skills"} {
		resp, m := do(t, srv, "GET", p, "")
		if resp.StatusCode != 200 {
			t.Errorf("%s status=%d", p, resp.StatusCode)
		}
		if m["registry_version"] == nil {
			t.Errorf("%s missing registry_version", p)
		}
	}
}

func TestReload(t *testing.T) {
	srv, tagsPath := newTestServer(t)
	// Append a new skill (valid change).
	cur, _ := os.ReadFile(tagsPath)
	changed := append(cur, []byte("\n  - skill: extra\n    agent: go-dev\n    tags: [clickhouse]\n    weight: 1.0\n")...)
	if err := os.WriteFile(tagsPath, changed, 0o644); err != nil {
		t.Fatal(err)
	}
	resp, m := do(t, srv, "POST", "/registry/reload", "")
	if resp.StatusCode != 200 {
		t.Fatalf("reload status=%d body=%v", resp.StatusCode, m)
	}
	if m["changed"] != true {
		t.Errorf("expected changed=true, got %v", m["changed"])
	}

	// Invalid change must be rejected (422).
	bad := append(changed, []byte("\n  - skill: bad\n    agent: ghost\n    tags: [go]\n")...)
	if err := os.WriteFile(tagsPath, bad, 0o644); err != nil {
		t.Fatal(err)
	}
	resp2, _ := do(t, srv, "POST", "/registry/reload", "")
	if resp2.StatusCode != 422 {
		t.Errorf("invalid reload status=%d want 422", resp2.StatusCode)
	}
}

func mustBody(resp *http.Response) string {
	b, _ := io.ReadAll(resp.Body)
	return string(b)
}

var _ = bytes.NewReader
var _ = mustBody
