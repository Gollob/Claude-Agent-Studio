// Package httpapi exposes the HTTP-only API of task-router on 127.0.0.1.
// It wires together parser, matcher, dispatcher, the in-memory registry (RCU)
// and the SQLite store. The service NEVER calls an LLM: slow-path returns a
// handoff contract for the orchestrator (ADR-005).
package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"

	"task-router/internal/dispatcher"
	"task-router/internal/matcher"
	"task-router/internal/parser"
	"task-router/internal/registry"
	"task-router/internal/store"
)

// logQueueSize bounds the in-flight routing_log backlog. On overflow the
// hot path drops the record (logged) rather than blocking the response.
const logQueueSize = 1024

// Request body size limits guard against unbounded reads / memory blowups.
const (
	maxRouteBody    = 1 << 20 // 1 MiB for /route and /triage/callback
	maxDispatchBody = 1 << 22 // 4 MiB for /dispatch (batches)
)

// logItem is one unit of work for the background log worker: either a routing
// entry to persist, or a flush request (ack closed once the queue is drained
// up to this point).
type logItem struct {
	entry store.RoutingEntry
	flush chan struct{} // non-nil => flush marker, close after preceding entries done
}

// tracer is the package-level OTel tracer for manual spans on key handlers.
// It returns the global no-op tracer when OTel is disabled — all span calls
// become zero-cost no-ops (graceful degradation).
var tracer = otel.Tracer("task-router/httpapi")

// Server holds dependencies for the HTTP handlers.
type Server struct {
	reg      *atomic.Pointer[registry.Registry]
	store    *store.Store
	tagsPath string
	log      *slog.Logger

	logCh  chan logItem  // buffered queue feeding the single log worker
	doneCh chan struct{} // closed when the worker goroutine has exited
}

// New constructs a Server and starts the background routing_log worker. reg
// must already hold a valid registry. Call Shutdown to drain and stop it.
func New(reg *atomic.Pointer[registry.Registry], st *store.Store, tagsPath string, log *slog.Logger) *Server {
	s := &Server{
		reg:      reg,
		store:    st,
		tagsPath: tagsPath,
		log:      log,
		logCh:    make(chan logItem, logQueueSize),
		doneCh:   make(chan struct{}),
	}
	go s.logWorker()
	return s
}

// logWorker is the single consumer of logCh. Writing routing_log off the hot
// path keeps /route, /dispatch and /triage/callback responses independent of
// SQLite write contention (busy_timeout). One worker also bounds goroutines.
func (s *Server) logWorker() {
	defer close(s.doneCh)
	for it := range s.logCh {
		if it.flush != nil {
			// All entries enqueued before this marker have been processed
			// (channel preserves FIFO order); signal the waiter.
			close(it.flush)
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		if err := s.store.AppendRoutingLog(ctx, it.entry); err != nil {
			s.log.Error("routing_log append failed", "err", err)
		}
		cancel()
	}
}

// Shutdown stops accepting new log entries, drains the queue and waits for the
// worker to finish persisting everything already enqueued.
func (s *Server) Shutdown(ctx context.Context) error {
	close(s.logCh)
	select {
	case <-s.doneCh:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// flushLog blocks until every routing_log entry enqueued before this call has
// been persisted. Used by tests (and /metrics) so reads observe prior writes.
func (s *Server) flushLog() {
	ack := make(chan struct{})
	select {
	case s.logCh <- logItem{flush: ack}:
		<-ack
	case <-s.doneCh:
		// Worker already stopped (post-Shutdown); nothing left to flush.
	}
}

// Handler returns the http.Handler with all routes registered.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /route", s.handleRoute)
	mux.HandleFunc("POST /dispatch", s.handleDispatch)
	mux.HandleFunc("POST /triage/callback", s.handleTriageCallback)
	mux.HandleFunc("POST /registry/reload", s.handleReload)
	mux.HandleFunc("POST /registry/sync", s.handleReload) // sync == reload on MVP
	mux.HandleFunc("GET /agents", s.handleAgents)
	mux.HandleFunc("GET /skills", s.handleSkills)
	mux.HandleFunc("GET /tags", s.handleTags)
	mux.HandleFunc("GET /healthz", s.handleHealthz)
	mux.HandleFunc("GET /metrics", s.handleMetrics)
	return mux
}

// ── helpers ────────────────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

// isSHA256Hex reports whether s is a 64-char lowercase hex string, i.e. the exact
// shape parser.HashText produces (hex.EncodeToString emits lowercase). Used to
// reject malformed text_hash values on the triage callback before they reach the
// cache, guarding against cache poisoning under a forged key.
func isSHA256Hex(s string) bool {
	if len(s) != 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func handoff(reg *registry.Registry) map[string]any {
	// allowed_tags_sample: deterministic sample of canonical tags.
	sample := reg.CanonOrder()
	if len(sample) > 20 {
		sample = sample[:20]
	}
	return map[string]any{
		"instruction":        "Analyze the task text, pick canonical tags from the registry, then POST /triage/callback {text_hash, tags}.",
		"allowed_tags_sample": sample,
		"expected_format":    "tags:[canon,...]",
	}
}

// ── POST /route ──────────────────────────────────────────────────────────────

type routeReq struct {
	Text     string   `json:"text"`
	Tags     []string `json:"tags"`
	PinAgent string   `json:"pin_agent"`
	TaskID   string   `json:"task_id"`
}

func (s *Server) handleRoute(w http.ResponseWriter, r *http.Request) {
	ctx, span := tracer.Start(r.Context(), "route.decision")
	defer span.End()
	r = r.WithContext(ctx)

	r.Body = http.MaxBytesReader(w, r.Body, maxRouteBody)
	var req routeReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		span.SetStatus(codes.Error, err.Error())
		writeErr(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	reg := s.reg.Load()
	start := time.Now()

	pr := parser.Parse(req.Text, req.Tags, reg)
	pin := req.PinAgent
	if pin == "" {
		pin = pr.PinAgent
	}

	// Triage cache: if slow-path but a cached triage exists for this hash, promote.
	usedCache := false
	if !pr.Fast && pin == "" {
		if entry, ok, err := s.store.GetTriage(r.Context(), pr.TextHash); err == nil && ok {
			if entry.RegistryVer == reg.Version {
				canon, _ := parser.NormalizeTags(entry.Tags, reg)
				if len(canon) > 0 {
					pr.CanonTags = canon
					pr.Fast = true
					usedCache = true
				}
			}
		}
	}

	// Slow-path: emit handoff contract, log, return.
	if !pr.Fast && pin == "" {
		latency := time.Since(start).Microseconds()
		span.SetAttributes(
			attribute.String("route.path", "slow"),
			attribute.String("route.status", "needs_triage"),
			attribute.String("route.text_hash", pr.TextHash),
			attribute.Int64("route.latency_us", latency),
		)
		s.logRouting(store.RoutingEntry{
			TextHash: pr.TextHash, Tags: pr.CanonTags, Path: "slow",
			Candidates: nil, Chosen: "", UsedLLM: false, Confidence: 0,
			LatencyUS: latency, RegistryVer: reg.Version,
		})
		writeJSON(w, http.StatusOK, map[string]any{
			"path":         "slow",
			"task_id":      req.TaskID,
			"status":       "needs_triage",
			"text_hash":    pr.TextHash,
			"unknown_tags": pr.UnknownTags,
			"used_llm":     false,
			"handoff":      handoff(reg),
		})
		return
	}

	// Fast-path: match.
	mr := matcher.Match(pr.CanonTags, pin, 10, reg)

	// Auto-escalate to slow-path on no_match or low confidence (no pin).
	if pin == "" && (mr.NoMatch || mr.Confidence < matcher.MinConfidence) {
		latency := time.Since(start).Microseconds()
		span.SetAttributes(
			attribute.String("route.path", "slow"),
			attribute.String("route.status", "needs_triage"),
			attribute.String("route.escalate_reason", escalateReason(mr)),
			attribute.Float64("route.confidence", float64(mr.Confidence)),
			attribute.Int64("route.latency_us", latency),
		)
		s.logRouting(store.RoutingEntry{
			TextHash: pr.TextHash, Tags: pr.CanonTags, Path: "slow",
			Candidates: mr.Candidates, Chosen: "", UsedLLM: false,
			Confidence: mr.Confidence, LatencyUS: latency, RegistryVer: reg.Version,
		})
		writeJSON(w, http.StatusOK, map[string]any{
			"path":           "slow",
			"task_id":        req.TaskID,
			"status":         "needs_triage",
			"text_hash":      pr.TextHash,
			"tags":           pr.CanonTags,
			"no_match":       mr.NoMatch,
			"confidence":     mr.Confidence,
			"uncovered_tags": mr.UncoveredTags,
			"used_llm":       false,
			"handoff":        handoff(reg),
			"reason":         escalateReason(mr),
		})
		return
	}

	latency := time.Since(start).Microseconds()
	span.SetAttributes(
		attribute.String("route.path", "fast"),
		attribute.String("route.chosen", mr.Chosen),
		attribute.Float64("route.confidence", float64(mr.Confidence)),
		attribute.Int64("route.latency_us", latency),
		attribute.Bool("route.from_cache", usedCache),
	)
	s.logRouting(store.RoutingEntry{
		TextHash: pr.TextHash, Tags: pr.CanonTags, Path: "fast",
		Candidates: mr.Candidates, Chosen: mr.Chosen, UsedLLM: false,
		Confidence: mr.Confidence, LatencyUS: latency, RegistryVer: reg.Version,
	})
	writeJSON(w, http.StatusOK, map[string]any{
		"path":           "fast",
		"task_id":        req.TaskID,
		"tags":           pr.CanonTags,
		"unknown_tags":   pr.UnknownTags,
		"candidates":     mr.Candidates,
		"chosen":         mr.Chosen,
		"covered_tags":   mr.CoveredTags,
		"uncovered_tags": mr.UncoveredTags,
		"confidence":     mr.Confidence,
		"pin_applied":    mr.PinApplied,
		"no_match":       mr.NoMatch,
		"used_llm":       false,
		"from_cache":     usedCache,
		"latency_us":     latency,
	})
}

func escalateReason(mr matcher.Result) string {
	if mr.NoMatch {
		return "no agent covers the tags (signal skill-curator)"
	}
	return fmt.Sprintf("low confidence %.2f < %.2f", mr.Confidence, matcher.MinConfidence)
}

// ── POST /dispatch ───────────────────────────────────────────────────────────

type dispatchReq struct {
	Tasks       []dispatcher.TaskInput `json:"tasks"`
	MaxParallel int                    `json:"max_parallel"`
}

func (s *Server) handleDispatch(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxDispatchBody)
	var req dispatchReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	reg := s.reg.Load()
	start := time.Now()

	plan := dispatcher.Build(req.Tasks, req.MaxParallel, reg)

	// Log each scheduled assignment to routing_log (fast-path decisions).
	for _, wave := range plan.Waves {
		for _, a := range wave {
			s.logRouting(store.RoutingEntry{
				Tags: a.Tags, Path: "fast", Chosen: a.Agent,
				Candidates: map[string]any{"agent": a.Agent, "score": a.Score},
				UsedLLM:    false, Confidence: a.Confidence,
				LatencyUS:   time.Since(start).Microseconds(), RegistryVer: reg.Version,
			})
		}
	}
	// Also log slow-path records for tasks that need triage, so batch KPI
	// (slow_path_total, fast/used_llm ratios) stays correct for /dispatch.
	for _, nt := range plan.NeedsTriage {
		s.logRouting(store.RoutingEntry{
			Tags: nil, Path: "slow", Chosen: "", UsedLLM: false, Confidence: 0,
			LatencyUS: time.Since(start).Microseconds(), RegistryVer: reg.Version,
		})
		_ = nt
	}
	writeJSON(w, http.StatusOK, plan)
}

// ── POST /triage/callback ─────────────────────────────────────────────────────

type triageReq struct {
	TaskID string `json:"task_id"`
	Text   string `json:"text"`
	// TextHash closes the triage loop without echoing the full task text: the
	// orchestrator takes it verbatim from the /route slow-path response. Used as
	// the cache key when Text is empty.
	TextHash string   `json:"text_hash"`
	Tags     []string `json:"tags"`
	// UsedLLM marks whether the tags came from an LLM triage. A callback is
	// usually the result of LLM-assisted triage, so it defaults to true when
	// the field is omitted; the orchestrator may pass false when tags were
	// resolved deterministically (e.g. from its own cache).
	UsedLLM *bool `json:"used_llm,omitempty"`
}

func (s *Server) handleTriageCallback(w http.ResponseWriter, r *http.Request) {
	ctx, span := tracer.Start(r.Context(), "triage.callback")
	defer span.End()
	r = r.WithContext(ctx)

	r.Body = http.MaxBytesReader(w, r.Body, maxRouteBody)
	var req triageReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		span.SetStatus(codes.Error, err.Error())
		writeErr(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	// Validate text_hash format BEFORE any normalization / cache write: an
	// attacker-supplied or mistyped hash could otherwise poison the triage cache
	// under someone else's key, causing /route to return the wrong agent. A
	// non-empty text_hash must be exactly the 64-char lowercase sha256 hex that
	// parser.HashText emits (see isSHA256Hex). The full-text path needs no check
	// because we compute the hash ourselves.
	if req.TextHash != "" && !isSHA256Hex(req.TextHash) {
		writeErr(w, http.StatusBadRequest, "text_hash must be 64-char sha256 hex")
		return
	}
	reg := s.reg.Load()
	start := time.Now()
	canon, unknown := parser.NormalizeTags(req.Tags, reg)
	if len(canon) == 0 {
		writeErr(w, http.StatusUnprocessableEntity, "no canonical tags after normalization")
		return
	}

	usedLLM := true
	if req.UsedLLM != nil {
		usedLLM = *req.UsedLLM
	}

	// Determine the cache key (text_hash). Prefer hashing the full task text when
	// provided; otherwise use the text_hash echoed from the /route slow-path
	// response. This closes the triage loop so future identical tasks go fast.
	var hash string
	switch {
	case req.Text != "":
		hash = parser.HashText(req.Text)
	case req.TextHash != "":
		hash = req.TextHash
	}
	cached := false
	if hash != "" {
		// Tags are normalized against the CURRENT reg (parser.NormalizeTags above)
		// and the cache row is written with this reg.Version. So if a reload lands
		// between /route and this callback, the write stays self-consistent: a
		// later /route read only promotes the cache when entry.RegistryVer matches
		// the live reg.Version, so a stale-version row simply won't match (no
		// cross-version mixing).
		if err := s.store.UpsertTriage(r.Context(), hash, canon, reg.Version); err != nil {
			s.log.Error("triage cache upsert failed", "err", err)
		} else {
			cached = true
		}
	}

	// Complete the fast-path: match now.
	mr := matcher.Match(canon, "", 10, reg)
	latency := time.Since(start).Microseconds()
	span.SetAttributes(
		attribute.String("triage.chosen", mr.Chosen),
		attribute.Float64("triage.confidence", float64(mr.Confidence)),
		attribute.Bool("triage.cached", cached),
		attribute.Bool("triage.used_llm", usedLLM),
		attribute.Int64("triage.latency_us", latency),
	)
	s.logRouting(store.RoutingEntry{
		TextHash:   hash,
		Tags:       canon, Path: "fast", Chosen: mr.Chosen,
		Candidates: mr.Candidates, UsedLLM: usedLLM, Confidence: mr.Confidence,
		LatencyUS:   latency, RegistryVer: reg.Version,
	})
	writeJSON(w, http.StatusOK, map[string]any{
		"path":         "fast",
		"task_id":      req.TaskID,
		"tags":         canon,
		"unknown_tags": unknown,
		"candidates":   mr.Candidates,
		"chosen":       mr.Chosen,
		"confidence":   mr.Confidence,
		"no_match":     mr.NoMatch,
		"used_llm":     usedLLM,
		"cached":       cached,
	})
}

// ── POST /registry/reload ─────────────────────────────────────────────────────

func (s *Server) handleReload(w http.ResponseWriter, r *http.Request) {
	prev := ""
	if cur := s.reg.Load(); cur != nil {
		prev = cur.Version
	}
	if err := registry.Reload(s.tagsPath, s.reg); err != nil {
		writeErr(w, http.StatusUnprocessableEntity, "reload rejected: "+err.Error())
		return
	}
	cur := s.reg.Load()
	s.log.Info("registry reloaded", "version", cur.Version, "prev", prev)
	writeJSON(w, http.StatusOK, map[string]any{
		"status":       "ok",
		"version":      cur.Version,
		"prev_version": prev,
		"changed":      cur.Version != prev,
	})
}

// ── read-only views ───────────────────────────────────────────────────────────

func (s *Server) handleAgents(w http.ResponseWriter, _ *http.Request) {
	reg := s.reg.Load()
	out := make([]registry.AgentMeta, 0, len(reg.Agents))
	for _, name := range reg.AgentOrder() {
		out = append(out, reg.Agents[name])
	}
	writeJSON(w, http.StatusOK, map[string]any{"registry_version": reg.Version, "agents": out})
}

func (s *Server) handleTags(w http.ResponseWriter, _ *http.Request) {
	reg := s.reg.Load()
	out := make([]registry.TagMeta, 0, len(reg.Tags))
	for _, c := range reg.CanonOrder() {
		out = append(out, reg.Tags[c])
	}
	writeJSON(w, http.StatusOK, map[string]any{"registry_version": reg.Version, "tags": out})
}

type skillView struct {
	Skill       string  `json:"skill"`
	Agent       string  `json:"agent"`
	Tag         string  `json:"tag"`
	SkillWeight float32 `json:"skill_weight"`
}

func (s *Server) handleSkills(w http.ResponseWriter, _ *http.Request) {
	reg := s.reg.Load()
	out := make([]skillView, 0)
	for _, canon := range reg.CanonOrder() {
		for _, p := range reg.Postings[canon] {
			out = append(out, skillView{Skill: p.Skill, Agent: p.Agent, Tag: canon, SkillWeight: p.SkillWeight})
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"registry_version": reg.Version, "postings": out})
}

// ── GET /healthz ──────────────────────────────────────────────────────────────

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	reg := s.reg.Load()
	dbStatus := "ok"
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.store.Ping(ctx); err != nil {
		dbStatus = "down: " + err.Error()
	}
	// Service stays ready even if DB is down: hot path is RAM-only.
	writeJSON(w, http.StatusOK, map[string]any{
		"status":             "ok",
		"registry_version":   reg.Version,
		"registry_loaded_at": reg.LoadedAt.Format(time.RFC3339),
		"db_status":          dbStatus,
	})
}

// ── GET /metrics ──────────────────────────────────────────────────────────────

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	reg := s.reg.Load()
	// Drain pending async routing_log writes so the metrics reflect every
	// decision already returned to clients (avoids races between the hot path
	// and the aggregate read).
	s.flushLog()
	m, err := s.store.Aggregate(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "metrics aggregate: "+err.Error())
		return
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, "# HELP task_router_routes_total Total routing decisions logged.\n")
	fmt.Fprintf(w, "# TYPE task_router_routes_total counter\n")
	fmt.Fprintf(w, "task_router_routes_total %d\n", m.Total)
	fmt.Fprintf(w, "# HELP task_router_fast_path_ratio Fraction of decisions resolved fast-path.\n")
	fmt.Fprintf(w, "# TYPE task_router_fast_path_ratio gauge\n")
	fmt.Fprintf(w, "task_router_fast_path_ratio %.6f\n", m.FastPathRatio)
	fmt.Fprintf(w, "# HELP task_router_used_llm_total Decisions that required LLM triage.\n")
	fmt.Fprintf(w, "# TYPE task_router_used_llm_total counter\n")
	fmt.Fprintf(w, "task_router_used_llm_total %d\n", m.UsedLLMCount)
	fmt.Fprintf(w, "# HELP task_router_no_llm_total Decisions resolved without LLM triage.\n")
	fmt.Fprintf(w, "# TYPE task_router_no_llm_total counter\n")
	fmt.Fprintf(w, "task_router_no_llm_total %d\n", m.NoLLMCount)
	fmt.Fprintf(w, "# HELP task_router_used_llm_ratio Fraction of decisions that used LLM.\n")
	fmt.Fprintf(w, "# TYPE task_router_used_llm_ratio gauge\n")
	fmt.Fprintf(w, "task_router_used_llm_ratio %.6f\n", m.UsedLLMRatio)
	fmt.Fprintf(w, "# HELP task_router_slow_path_total Decisions that needed slow-path triage.\n")
	fmt.Fprintf(w, "# TYPE task_router_slow_path_total counter\n")
	fmt.Fprintf(w, "task_router_slow_path_total %d\n", m.SlowCount)
	fmt.Fprintf(w, "# HELP task_router_route_latency_us Routing latency percentiles (microseconds).\n")
	fmt.Fprintf(w, "# TYPE task_router_route_latency_us summary\n")
	fmt.Fprintf(w, "task_router_route_latency_us{quantile=\"0.5\"} %d\n", m.P50LatencyUS)
	fmt.Fprintf(w, "task_router_route_latency_us{quantile=\"0.99\"} %d\n", m.P99LatencyUS)
	fmt.Fprintf(w, "task_router_avg_confidence %.6f\n", m.AvgConfidence)
	fmt.Fprintf(w, "# HELP task_router_registry_info Active registry version.\n")
	fmt.Fprintf(w, "task_router_registry_info{version=\"%s\"} 1\n", reg.Version)
}

// logRouting enqueues a routing_log entry for the background worker. It never
// blocks the hot path: on a full queue the record is dropped (and logged).
func (s *Server) logRouting(e store.RoutingEntry) {
	select {
	case s.logCh <- logItem{entry: e}:
	default:
		s.log.Warn("routing_log queue full, dropping entry", "path", e.Path, "chosen", e.Chosen)
	}
}
