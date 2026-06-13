package parser

import (
	"reflect"
	"testing"

	"task-router/internal/registry"
)

const testYAML = `
tags:
  - canon: go
    aliases: [golang, go-lang]
    domain: golang
    weight: 1.2
  - canon: grpc
    aliases: [proto, protobuf]
    domain: golang
    weight: 1.1
  - canon: clickhouse
    aliases: [ch]
    domain: db
    weight: 1.2
  - canon: schema
    aliases: []
    domain: db
    weight: 1.0
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
  - skill: ch-schema
    agent: db-engineer
    tags: [clickhouse, schema]
    weight: 1.5
`

func testReg(t *testing.T) *registry.Registry {
	t.Helper()
	reg, err := registry.BuildRegistry([]byte(testYAML))
	if err != nil {
		t.Fatalf("build registry: %v", err)
	}
	return reg
}

func TestParse_ExplicitTags(t *testing.T) {
	reg := testReg(t)
	res := Parse("#go #grpc реализуй gateway", nil, reg)
	if !res.Fast {
		t.Errorf("expected fast path")
	}
	if !reflect.DeepEqual(res.CanonTags, []string{"go", "grpc"}) {
		t.Errorf("CanonTags=%v want [go grpc]", res.CanonTags)
	}
}

func TestParse_AliasNormalization(t *testing.T) {
	reg := testReg(t)
	res := Parse("#golang build", nil, reg)
	if !reflect.DeepEqual(res.CanonTags, []string{"go"}) {
		t.Errorf("CanonTags=%v want [go]", res.CanonTags)
	}
}

func TestParse_PinAgent(t *testing.T) {
	reg := testReg(t)
	res := Parse("@db-engineer сделай схему", nil, reg)
	if res.PinAgent != "db-engineer" {
		t.Errorf("PinAgent=%q want db-engineer", res.PinAgent)
	}
	if !res.Fast {
		t.Errorf("pin_agent should yield fast path")
	}
}

func TestParse_UnknownAgentIgnored(t *testing.T) {
	reg := testReg(t)
	res := Parse("@nobody do it", nil, reg)
	if res.PinAgent != "" {
		t.Errorf("unknown agent should not pin, got %q", res.PinAgent)
	}
	if res.Fast {
		t.Errorf("no tags + unknown pin => slow path")
	}
}

func TestParse_TagsFieldMerged(t *testing.T) {
	reg := testReg(t)
	res := Parse("#go build", []string{"clickhouse", "schema"}, reg)
	want := map[string]bool{"go": true, "clickhouse": true, "schema": true}
	if len(res.CanonTags) != 3 {
		t.Fatalf("CanonTags=%v want 3", res.CanonTags)
	}
	for _, c := range res.CanonTags {
		if !want[c] {
			t.Errorf("unexpected tag %q", c)
		}
	}
}

func TestParse_UnknownTag(t *testing.T) {
	reg := testReg(t)
	res := Parse("#квантовость do this", nil, reg)
	if res.Fast {
		t.Errorf("only unknown tags => slow path")
	}
	if len(res.UnknownTags) != 1 || res.UnknownTags[0] != "квантовость" {
		t.Errorf("UnknownTags=%v", res.UnknownTags)
	}
}

func TestParse_SlowPathNoTags(t *testing.T) {
	reg := testReg(t)
	res := Parse("просто сделай что-нибудь полезное", nil, reg)
	if res.Fast {
		t.Errorf("no tags, no agent => slow path expected")
	}
}

func TestParse_Domain(t *testing.T) {
	reg := testReg(t)
	res := Parse("задача в [clickhouse] окружении", nil, reg)
	if !reflect.DeepEqual(res.CanonTags, []string{"clickhouse"}) {
		t.Errorf("domain extraction failed: %v", res.CanonTags)
	}
}

func TestHashText_StableUnderVariation(t *testing.T) {
	a := HashText("Построй gateway, СРОЧНО!!!")
	b := HashText("построй   gateway срочно")
	if a != b {
		t.Errorf("normalized hash should be stable: %s vs %s", a, b)
	}
}

func TestParse_Dedup(t *testing.T) {
	reg := testReg(t)
	res := Parse("#go #go #golang", nil, reg)
	if len(res.CanonTags) != 1 {
		t.Errorf("expected dedup to single tag, got %v", res.CanonTags)
	}
}
