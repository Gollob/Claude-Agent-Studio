// Package dispatcher distributes a batch of tasks across agents and builds a
// deterministic fan-out plan of waves. It honors depends_on (topological order)
// and a heavy-agent parallelism cap (default 2, max 3) to respect the VM RAM
// budget. The dispatcher only PLANS — the orchestrator executes.
package dispatcher

import (
	"sort"

	"task-router/internal/matcher"
	"task-router/internal/parser"
	"task-router/internal/registry"
)

const (
	// DefaultMaxParallel is the default cap on simultaneous heavy agents per wave.
	DefaultMaxParallel = 2
	// MaxAllowedParallel is the hard ceiling on heavy agents per wave.
	MaxAllowedParallel = 3
)

// TaskInput is one task in a dispatch batch.
type TaskInput struct {
	TaskID    string   `json:"task_id"`
	Text      string   `json:"text"`
	Tags      []string `json:"tags,omitempty"`
	PinAgent  string   `json:"pin_agent,omitempty"`
	DependsOn []string `json:"depends_on,omitempty"`
}

// Assignment is a planned task->agent decision.
type Assignment struct {
	TaskID     string   `json:"task_id"`
	Agent      string   `json:"agent"`
	Score      float64  `json:"score"`
	Heavy      bool     `json:"heavy"`
	Tags       []string `json:"tags"`
	Confidence float64  `json:"confidence"`
}

// NeedsTriage is a task that could not be matched fast-path.
type NeedsTriage struct {
	TaskID string `json:"task_id"`
	Reason string `json:"reason"`
}

// Plan is the dispatcher output: ordered waves + triage list + rationale.
type Plan struct {
	Waves       [][]Assignment `json:"waves"`
	NeedsTriage []NeedsTriage  `json:"needs_triage"`
	Rationale   string         `json:"rationale"`
	MaxParallel int            `json:"max_parallel"`
}

// Plan builds a deterministic fan-out plan for the batch.
func Build(tasks []TaskInput, maxParallel int, reg *registry.Registry) Plan {
	if maxParallel <= 0 {
		maxParallel = DefaultMaxParallel
	}
	if maxParallel > MaxAllowedParallel {
		maxParallel = MaxAllowedParallel
	}

	plan := Plan{MaxParallel: maxParallel}

	// 1. Match each task; split into assignments vs needs_triage.
	assigns := make(map[string]Assignment) // taskID -> assignment (fast-path)
	for _, t := range tasks {
		pr := parser.Parse(t.Text, t.Tags, reg)
		pin := t.PinAgent
		if pin == "" {
			pin = pr.PinAgent
		}
		if !pr.Fast && pin == "" {
			plan.NeedsTriage = append(plan.NeedsTriage, NeedsTriage{
				TaskID: t.TaskID,
				Reason: "no canonical tags and no pin_agent",
			})
			continue
		}
		mr := matcher.Match(pr.CanonTags, pin, 5, reg)
		if mr.NoMatch && pin == "" {
			plan.NeedsTriage = append(plan.NeedsTriage, NeedsTriage{
				TaskID: t.TaskID,
				Reason: "no agent covers the tags",
			})
			continue
		}
		heavy := false
		if am, ok := reg.Agents[mr.Chosen]; ok {
			heavy = am.Heavy
		}
		assigns[t.TaskID] = Assignment{
			TaskID:     t.TaskID,
			Agent:      mr.Chosen,
			Score:      score(mr),
			Heavy:      heavy,
			Tags:       pr.CanonTags,
			Confidence: mr.Confidence,
		}
	}

	// 2. Topological waves over depends_on, honoring heavy cap.
	plan.Waves = schedule(tasks, assigns, maxParallel)
	plan.Rationale = rationale(plan)
	return plan
}

// score returns the chosen candidate's score (or 0).
func score(mr matcher.Result) float64 {
	for _, c := range mr.Candidates {
		if c.Agent == mr.Chosen {
			return c.Score
		}
	}
	return 0
}

// schedule builds waves via Kahn-style topological layering with a heavy cap.
// Deterministic: ready tasks are taken by (score desc, task_id asc).
func schedule(tasks []TaskInput, assigns map[string]Assignment, maxParallel int) [][]Assignment {
	// Only schedule tasks that got an assignment (others went to needs_triage).
	scheduled := make(map[string]bool, len(assigns))
	for id := range assigns {
		scheduled[id] = true
	}

	// Build dependency sets restricted to scheduled tasks.
	deps := make(map[string]map[string]struct{}, len(assigns))
	for _, t := range tasks {
		if !scheduled[t.TaskID] {
			continue
		}
		set := make(map[string]struct{})
		for _, d := range t.DependsOn {
			// ignore deps on unscheduled (triaged/absent) tasks — can't wait forever
			if scheduled[d] {
				set[d] = struct{}{}
			}
		}
		deps[t.TaskID] = set
	}

	done := make(map[string]bool)
	var waves [][]Assignment

	for len(done) < len(scheduled) {
		// Collect ready tasks: all deps done, not yet done.
		var ready []Assignment
		for id := range scheduled {
			if done[id] {
				continue
			}
			allDepsDone := true
			for d := range deps[id] {
				if !done[d] {
					allDepsDone = false
					break
				}
			}
			if allDepsDone {
				ready = append(ready, assigns[id])
			}
		}
		if len(ready) == 0 {
			// Cycle or unresolved deps: break to avoid infinite loop (deterministic).
			break
		}
		// Deterministic order: score desc, then task_id asc.
		sort.Slice(ready, func(i, j int) bool {
			if ready[i].Score != ready[j].Score {
				return ready[i].Score > ready[j].Score
			}
			return ready[i].TaskID < ready[j].TaskID
		})

		// Greedily fill a wave with heavy cap.
		var wave []Assignment
		heavyCount := 0
		for _, a := range ready {
			if a.Heavy {
				if heavyCount >= maxParallel {
					continue
				}
				heavyCount++
			}
			wave = append(wave, a)
		}
		for _, a := range wave {
			done[a.TaskID] = true
		}
		waves = append(waves, wave)
	}
	return waves
}

func rationale(p Plan) string {
	switch {
	case len(p.Waves) == 0 && len(p.NeedsTriage) > 0:
		return "all tasks require triage"
	case len(p.Waves) == 0:
		return "no tasks to schedule"
	default:
		return "fan-out plan with heavy-agent cap honoring depends_on"
	}
}
