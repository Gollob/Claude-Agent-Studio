package matcher

import (
	"fmt"
	"sort"
	"testing"

	"task-router/internal/registry"
)

const matchYAML = `
tags:
  - canon: go
    domain: golang
    weight: 1.2
  - canon: grpc
    domain: golang
    weight: 1.1
  - canon: observability
    domain: devops
    weight: 1.0
  - canon: test
    domain: qa
    weight: 1.0
agents:
  - agent: go-dev
    model: opus
    heavy: 1
  - agent: devops
    model: sonnet
    heavy: 0
  - agent: qa-test
    model: sonnet
    heavy: 0
skills:
  - skill: golang-grpc
    agent: go-dev
    tags: [go, grpc]
    weight: 1.8
  - skill: golang-observability
    agent: go-dev
    tags: [go, observability]
    weight: 1.5
  - skill: monitoring
    agent: devops
    tags: [observability]
    weight: 1.6
  - skill: testing
    agent: qa-test
    tags: [test]
    weight: 2.0
`

func matchReg(t *testing.T) *registry.Registry {
	t.Helper()
	reg, err := registry.BuildRegistry([]byte(matchYAML))
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	return reg
}

func TestMatch_Ranking(t *testing.T) {
	reg := matchReg(t)
	res := Match([]string{"go", "grpc", "observability"}, "", 10, reg)
	if res.Chosen != "go-dev" {
		t.Errorf("chosen=%q want go-dev", res.Chosen)
	}
	if res.NoMatch {
		t.Errorf("unexpected no_match")
	}
	// go-dev covers go,grpc,observability; devops covers only observability.
	if len(res.Candidates) < 2 {
		t.Fatalf("expected >=2 candidates")
	}
	if res.Candidates[0].Agent != "go-dev" {
		t.Errorf("top candidate=%q want go-dev", res.Candidates[0].Agent)
	}
}

func TestMatch_DeterministicGolden(t *testing.T) {
	reg := matchReg(t)
	// Run many times; order must be identical (golden).
	var golden []string
	for i := 0; i < 50; i++ {
		res := Match([]string{"observability", "go", "test", "grpc"}, "", 10, reg)
		order := make([]string, len(res.Candidates))
		for j, c := range res.Candidates {
			order[j] = fmt.Sprintf("%s:%.4f", c.Agent, c.Score)
		}
		if golden == nil {
			golden = order
			continue
		}
		if len(order) != len(golden) {
			t.Fatalf("candidate count changed across runs")
		}
		for j := range order {
			if order[j] != golden[j] {
				t.Fatalf("non-deterministic order at iter %d: %v vs golden %v", i, order, golden)
			}
		}
	}
	t.Logf("golden order: %v", golden)
}

func TestMatch_TieBreakByAgent(t *testing.T) {
	// Two agents with identical score must order by agent name asc.
	y := `
tags:
  - canon: x
    domain: golang
    weight: 1.0
agents:
  - agent: zeta
    model: sonnet
  - agent: alpha
    model: sonnet
skills:
  - skill: s1
    agent: zeta
    tags: [x]
    weight: 1.0
  - skill: s2
    agent: alpha
    tags: [x]
    weight: 1.0
`
	reg, err := registry.BuildRegistry([]byte(y))
	if err != nil {
		t.Fatal(err)
	}
	res := Match([]string{"x"}, "", 10, reg)
	if res.Candidates[0].Agent != "alpha" {
		t.Errorf("tie-break failed: top=%q want alpha", res.Candidates[0].Agent)
	}
	if res.Chosen != "alpha" {
		t.Errorf("chosen=%q want alpha (tie-break)", res.Chosen)
	}
}

func TestMatch_PinAgent(t *testing.T) {
	reg := matchReg(t)
	res := Match([]string{"go"}, "qa-test", 10, reg)
	if res.Chosen != "qa-test" {
		t.Errorf("pin ignored: chosen=%q", res.Chosen)
	}
	if !res.PinApplied {
		t.Errorf("PinApplied should be true")
	}
	// qa-test covers none of [go] -> confidence 0.
	if res.Confidence != 0 {
		t.Errorf("expected 0 confidence for non-covering pin, got %f", res.Confidence)
	}
}

func TestMatch_NoMatch(t *testing.T) {
	// A canonical tag present in the registry but covered by no skill (no postings).
	y2 := `
tags:
  - canon: orphan
    domain: golang
    weight: 1.0
agents:
  - agent: go-dev
    model: opus
skills: []
`
	reg2, err := registry.BuildRegistry([]byte(y2))
	if err != nil {
		t.Fatal(err)
	}
	res := Match([]string{"orphan"}, "", 10, reg2)
	if !res.NoMatch {
		t.Errorf("expected no_match for orphan tag")
	}
}

// TestMatch_InactiveTagIgnored verifies that an inactive tag (is_active:false)
// contributes nothing to any agent's score, even though the skill posting for
// it still exists in the active agent's index. Querying only the inactive tag
// must yield no_match.
func TestMatch_InactiveTagIgnored(t *testing.T) {
	y := `
tags:
  - canon: active
    domain: golang
    weight: 1.0
  - canon: dead
    domain: golang
    weight: 5.0
    is_active: false
agents:
  - agent: go-dev
    model: opus
skills:
  - skill: s1
    agent: go-dev
    tags: [active, dead]
    weight: 2.0
`
	reg, err := registry.BuildRegistry([]byte(y))
	if err != nil {
		t.Fatal(err)
	}

	// Query ONLY the inactive tag => it must not contribute => no match.
	resDead := Match([]string{"dead"}, "", 10, reg)
	if !resDead.NoMatch {
		t.Errorf("inactive tag produced a match: chosen=%q score affected", resDead.Chosen)
	}

	// Query active + dead: score must equal the active-only score (dead adds 0).
	resBoth := Match([]string{"active", "dead"}, "", 10, reg)
	resActive := Match([]string{"active"}, "", 10, reg)
	if len(resActive.Candidates) == 0 || len(resBoth.Candidates) == 0 {
		t.Fatalf("expected go-dev to cover the active tag")
	}
	if resBoth.Candidates[0].Score != resActive.Candidates[0].Score {
		t.Errorf("inactive tag changed the score: both=%.4f active-only=%.4f",
			resBoth.Candidates[0].Score, resActive.Candidates[0].Score)
	}
	// "dead" must appear as uncovered (it contributed nothing).
	foundDeadCovered := false
	for _, c := range resBoth.CoveredTags {
		if c == "dead" {
			foundDeadCovered = true
		}
	}
	if foundDeadCovered {
		t.Errorf("inactive tag 'dead' reported as covered: %v", resBoth.CoveredTags)
	}
}

func TestMatch_Explainability(t *testing.T) {
	reg := matchReg(t)
	// go-dev covers go,grpc; "test" is uncovered by go-dev.
	res := Match([]string{"go", "grpc", "test"}, "", 10, reg)
	// covered/uncovered are relative to ANY agent. test IS covered (qa-test).
	if len(res.CoveredTags) != 3 {
		t.Errorf("CoveredTags=%v expected all 3 covered by some agent", res.CoveredTags)
	}
	if res.Confidence <= 0 || res.Confidence > 1 {
		t.Errorf("confidence out of range: %f", res.Confidence)
	}
}

// ── benchmark on synthetic 1000 agents × 1000 skills ──────────────────────

func buildSyntheticRegistry(nTags, nAgents, nSkills int) *registry.Registry {
	var b []byte
	w := func(s string) { b = append(b, s...) }
	w("tags:\n")
	for i := 0; i < nTags; i++ {
		w(fmt.Sprintf("  - canon: tag%d\n    domain: golang\n    weight: 1.0\n", i))
	}
	w("agents:\n")
	for i := 0; i < nAgents; i++ {
		w(fmt.Sprintf("  - agent: agent%d\n    model: sonnet\n    heavy: 0\n", i))
	}
	w("skills:\n")
	for i := 0; i < nSkills; i++ {
		// each skill owned by agent i%nAgents, with 5 tags spread across the catalog.
		ag := i % nAgents
		w(fmt.Sprintf("  - skill: skill%d\n    agent: agent%d\n    weight: 1.0\n    tags: [", i, ag))
		for k := 0; k < 5; k++ {
			if k > 0 {
				w(", ")
			}
			w(fmt.Sprintf("tag%d", (i*7+k*13)%nTags))
		}
		w("]\n")
	}
	reg, err := registry.BuildRegistry(b)
	if err != nil {
		panic(err)
	}
	return reg
}

func BenchmarkMatch1000x1000(b *testing.B) {
	reg := buildSyntheticRegistry(1000, 1000, 1000)
	// Pick 10 tags as a query (max realistic load).
	query := make([]string, 10)
	for i := range query {
		query[i] = fmt.Sprintf("tag%d", i*97%1000)
	}
	b.ReportAllocs()
	b.ResetTimer()

	lats := make([]int64, 0, b.N)
	for i := 0; i < b.N; i++ {
		res := Match(query, "", 10, reg)
		if res.Chosen == "" && !res.NoMatch {
			b.Fatal("unexpected empty result")
		}
	}
	_ = lats
}

// TestMatchLatencyP99 measures p99 of Match on the synthetic 1000x1000 registry.
func TestMatchLatencyP99(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping latency test in short mode")
	}
	reg := buildSyntheticRegistry(1000, 1000, 1000)
	query := make([]string, 10)
	for i := range query {
		query[i] = fmt.Sprintf("tag%d", i*97%1000)
	}
	// Warm up.
	for i := 0; i < 1000; i++ {
		Match(query, "", 10, reg)
	}
	const N = 20000
	samples := make([]int64, N)
	for i := 0; i < N; i++ {
		start := nowNano()
		Match(query, "", 10, reg)
		samples[i] = nowNano() - start
	}
	sort.Slice(samples, func(i, j int) bool { return samples[i] < samples[j] })
	p99 := samples[int(0.99*float64(N))]
	p50 := samples[N/2]
	t.Logf("Match p50=%dns p99=%dns (%.3fµs / %.3fµs) over %d samples, query=%d tags, 1000 agents x 1000 skills",
		p50, p99, float64(p50)/1000, float64(p99)/1000, N, len(query))
	if p99 > 1_000_000 { // 1 ms in ns
		t.Errorf("p99 %dns exceeds 1ms target", p99)
	}
}
