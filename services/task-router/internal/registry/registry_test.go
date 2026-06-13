package registry

import (
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
)

const validYAML = `
tags:
  - canon: go
    aliases: [golang, go-lang]
    domain: golang
    weight: 1.2
    is_active: true
  - canon: grpc
    aliases: [proto, protobuf]
    domain: golang
    weight: 1.1
    is_active: true
  - canon: test
    aliases: [testing, qa]
    domain: qa
    weight: 1.0
    is_active: true
agents:
  - agent: go-dev
    model: opus
    heavy: 1
    is_active: true
  - agent: qa-test
    model: sonnet
    heavy: 0
    is_active: true
skills:
  - skill: golang-grpc
    agent: go-dev
    tags: [go, grpc]
    weight: 1.8
  - skill: golang-testing
    agent: go-dev
    tags: [go, test]
    weight: 1.4
  - skill: test-master
    agent: qa-test
    tags: [test]
    weight: 2.0
`

func mustBuild(t *testing.T, y string) *Registry {
	t.Helper()
	reg, err := BuildRegistry([]byte(y))
	if err != nil {
		t.Fatalf("BuildRegistry: %v", err)
	}
	return reg
}

func TestBuildRegistry_AliasCanon(t *testing.T) {
	reg := mustBuild(t, validYAML)
	cases := map[string]string{
		"golang": "go", "go-lang": "go", "go": "go",
		"proto": "grpc", "GRPC": "grpc",
		"testing": "test", "qa": "test",
	}
	for in, want := range cases {
		got, ok := reg.Normalize(in)
		if !ok || got != want {
			t.Errorf("Normalize(%q) = %q,%v; want %q", in, got, ok, want)
		}
	}
	if _, ok := reg.Normalize("unknowntag"); ok {
		t.Errorf("unknown tag should not normalize")
	}
}

func TestBuildRegistry_Postings(t *testing.T) {
	reg := mustBuild(t, validYAML)
	// tag "test" covered by go-dev (golang-testing) and qa-test (test-master).
	ps := reg.Postings["test"]
	if len(ps) != 2 {
		t.Fatalf("Postings[test] len=%d want 2", len(ps))
	}
	// deterministic order: agent asc -> go-dev before qa-test.
	if ps[0].Agent != "go-dev" || ps[1].Agent != "qa-test" {
		t.Errorf("postings order not deterministic: %+v", ps)
	}
}

func TestBuildRegistry_Deterministic(t *testing.T) {
	a := mustBuild(t, validYAML)
	b := mustBuild(t, validYAML)
	if a.Version != b.Version {
		t.Fatalf("versions differ: %s vs %s", a.Version, b.Version)
	}
	for canon, psA := range a.Postings {
		psB := b.Postings[canon]
		if len(psA) != len(psB) {
			t.Fatalf("postings len mismatch for %q", canon)
		}
		for i := range psA {
			if psA[i] != psB[i] {
				t.Errorf("posting[%d] for %q differs: %+v vs %+v", i, canon, psA[i], psB[i])
			}
		}
	}
}

func TestBuildRegistry_RejectUnknownTag(t *testing.T) {
	bad := validYAML + `
  - skill: ghost
    agent: go-dev
    tags: [nonexistent]
    weight: 1.0
`
	if _, err := BuildRegistry([]byte(bad)); err == nil {
		t.Fatal("expected error for tag absent from registry")
	}
}

func TestBuildRegistry_RejectUnknownAgent(t *testing.T) {
	bad := validYAML + `
  - skill: ghost
    agent: nobody
    tags: [go]
    weight: 1.0
`
	if _, err := BuildRegistry([]byte(bad)); err == nil {
		t.Fatal("expected error for unknown agent")
	}
}

func TestBuildRegistry_RejectDupCanon(t *testing.T) {
	bad := `
tags:
  - canon: go
    domain: golang
  - canon: go
    domain: golang
agents:
  - agent: go-dev
    model: opus
skills: []
`
	if _, err := BuildRegistry([]byte(bad)); err == nil {
		t.Fatal("expected error for duplicate canon")
	}
}

func TestBuildRegistry_RejectDupAlias(t *testing.T) {
	bad := `
tags:
  - canon: go
    aliases: [shared]
    domain: golang
  - canon: python
    aliases: [shared]
    domain: python
agents: []
skills: []
`
	if _, err := BuildRegistry([]byte(bad)); err == nil {
		t.Fatal("expected error for duplicate alias")
	}
}

func TestReload_RCU(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "tags.yaml")
	if err := os.WriteFile(path, []byte(validYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	var ptr atomic.Pointer[Registry]
	if err := Reload(path, &ptr); err != nil {
		t.Fatalf("initial reload: %v", err)
	}
	v1 := ptr.Load().Version

	// No-op reload (same content).
	if err := Reload(path, &ptr); err != nil {
		t.Fatalf("noop reload: %v", err)
	}
	if ptr.Load().Version != v1 {
		t.Errorf("noop reload changed version")
	}

	// Invalid file must NOT replace active index.
	if err := os.WriteFile(path, []byte("tags:\n  - canon: go\n    tags: [missing]\nskills:\n  - skill: x\n    agent: ghost\n    tags: [missing]\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := Reload(path, &ptr); err == nil {
		t.Fatal("expected reload to reject invalid file")
	}
	if ptr.Load().Version != v1 {
		t.Errorf("invalid reload replaced active index (version changed)")
	}

	// Valid change swaps.
	changed := validYAML + `
  - skill: extra
    agent: qa-test
    tags: [go]
    weight: 1.0
`
	if err := os.WriteFile(path, []byte(changed), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := Reload(path, &ptr); err != nil {
		t.Fatalf("valid reload: %v", err)
	}
	if ptr.Load().Version == v1 {
		t.Errorf("valid reload did not change version")
	}
}

// TestRealRegistry ensures the shipped tags.yaml is valid.
func TestRealRegistry(t *testing.T) {
	reg, err := LoadFromFile("../../tagcatalog/tags.yaml")
	if err != nil {
		t.Fatalf("real tags.yaml invalid: %v", err)
	}
	if len(reg.Agents) < 9 {
		t.Errorf("expected >=9 agents, got %d", len(reg.Agents))
	}
	if _, ok := reg.Normalize("golang"); !ok {
		t.Errorf("golang alias missing in real registry")
	}
}
