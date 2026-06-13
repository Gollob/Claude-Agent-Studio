package dispatcher

import (
	"reflect"
	"testing"

	"task-router/internal/registry"
)

const dispYAML = `
tags:
  - canon: go
    domain: golang
    weight: 1.2
  - canon: arch
    domain: orchestration
    weight: 1.3
  - canon: test
    domain: qa
    weight: 1.0
  - canon: docs
    domain: docs
    weight: 0.9
agents:
  - agent: go-dev
    model: opus
    heavy: 1
  - agent: architect
    model: opus
    heavy: 1
  - agent: qa-test
    model: sonnet
    heavy: 0
  - agent: docs
    model: sonnet
    heavy: 0
skills:
  - skill: golang
    agent: go-dev
    tags: [go]
    weight: 1.5
  - skill: architect
    agent: architect
    tags: [arch]
    weight: 2.0
  - skill: testing
    agent: qa-test
    tags: [test]
    weight: 2.0
  - skill: docs
    agent: docs
    tags: [docs]
    weight: 2.0
`

func dispReg(t *testing.T) *registry.Registry {
	t.Helper()
	reg, err := registry.BuildRegistry([]byte(dispYAML))
	if err != nil {
		t.Fatal(err)
	}
	return reg
}

func TestBuild_MixedBatch(t *testing.T) {
	reg := dispReg(t)
	tasks := []TaskInput{
		{TaskID: "t1", Text: "#go build"},
		{TaskID: "t2", Text: "#test verify"},
		{TaskID: "t3", Text: "просто текст без тегов"},
		{TaskID: "t4", Text: "#docs write"},
	}
	plan := Build(tasks, 2, reg)
	if len(plan.NeedsTriage) != 1 || plan.NeedsTriage[0].TaskID != "t3" {
		t.Errorf("needs_triage=%+v want only t3", plan.NeedsTriage)
	}
	scheduled := 0
	for _, w := range plan.Waves {
		scheduled += len(w)
	}
	if scheduled != 3 {
		t.Errorf("scheduled=%d want 3", scheduled)
	}
}

func TestBuild_HeavyCap(t *testing.T) {
	reg := dispReg(t)
	// 4 heavy tasks (go-dev + architect are heavy), cap 2.
	tasks := []TaskInput{
		{TaskID: "a", Text: "#go x"},
		{TaskID: "b", Text: "#arch y"},
		{TaskID: "c", Text: "#go z"},
		{TaskID: "d", Text: "#arch w"},
	}
	plan := Build(tasks, 2, reg)
	for i, wave := range plan.Waves {
		heavy := 0
		for _, a := range wave {
			if a.Heavy {
				heavy++
			}
		}
		if heavy > 2 {
			t.Errorf("wave %d has %d heavy > cap 2", i, heavy)
		}
	}
}

func TestBuild_LightDoesNotCountAgainstHeavyCap(t *testing.T) {
	reg := dispReg(t)
	tasks := []TaskInput{
		{TaskID: "h1", Text: "#go a"},
		{TaskID: "h2", Text: "#arch b"},
		{TaskID: "l1", Text: "#test c"},
		{TaskID: "l2", Text: "#docs d"},
	}
	plan := Build(tasks, 2, reg)
	// All 4 fit in one wave: 2 heavy + 2 light.
	if len(plan.Waves) != 1 {
		t.Errorf("expected single wave, got %d: %+v", len(plan.Waves), plan.Waves)
	}
}

func TestBuild_DependsOn(t *testing.T) {
	reg := dispReg(t)
	tasks := []TaskInput{
		{TaskID: "b", Text: "#test b", DependsOn: []string{"a"}},
		{TaskID: "a", Text: "#go a"},
	}
	plan := Build(tasks, 2, reg)
	// a must appear in an earlier wave than b.
	waveOf := map[string]int{}
	for i, w := range plan.Waves {
		for _, as := range w {
			waveOf[as.TaskID] = i
		}
	}
	if waveOf["a"] >= waveOf["b"] {
		t.Errorf("dependency violated: a wave=%d b wave=%d", waveOf["a"], waveOf["b"])
	}
}

func TestBuild_Reproducible(t *testing.T) {
	reg := dispReg(t)
	tasks := []TaskInput{
		{TaskID: "t1", Text: "#go a"},
		{TaskID: "t2", Text: "#arch b"},
		{TaskID: "t3", Text: "#test c"},
		{TaskID: "t4", Text: "#docs d"},
		{TaskID: "t5", Text: "#go e"},
	}
	p1 := Build(tasks, 2, reg)
	p2 := Build(tasks, 2, reg)
	if !reflect.DeepEqual(p1.Waves, p2.Waves) {
		t.Errorf("plan not reproducible:\n%+v\n%+v", p1.Waves, p2.Waves)
	}
}

func TestBuild_MaxParallelClamp(t *testing.T) {
	reg := dispReg(t)
	plan := Build([]TaskInput{{TaskID: "x", Text: "#go a"}}, 99, reg)
	if plan.MaxParallel != MaxAllowedParallel {
		t.Errorf("max_parallel=%d want clamped to %d", plan.MaxParallel, MaxAllowedParallel)
	}
}
