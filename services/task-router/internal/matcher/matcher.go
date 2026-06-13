// Package matcher ranks agents against a task's canonical tags using only the
// in-memory inverted index (registry.Registry). The DB is NEVER touched on this
// hot path. Matching is deterministic: same input -> same candidate order
// (tie-break by agent name asc).
package matcher

import (
	"sort"

	"task-router/internal/registry"
)

// Candidate is a ranked agent with explainability data.
type Candidate struct {
	Agent        string   `json:"agent"`
	Score        float64  `json:"score"`
	CoveredTags  []string `json:"covered_tags"`
	Skills       []string `json:"skills"`
	TagScores    map[string]float64 `json:"tag_scores,omitempty"`
}

// Result is the outcome of matching.
type Result struct {
	Candidates    []Candidate `json:"candidates"`
	Chosen        string      `json:"chosen"`
	CoveredTags   []string    `json:"covered_tags"`
	UncoveredTags []string    `json:"uncovered_tags"`
	Confidence    float64     `json:"confidence"`
	NoMatch       bool        `json:"no_match"`
	PinApplied    bool        `json:"pin_applied,omitempty"`
}

// minConfidence below which the router should auto-escalate to slow-path.
const MinConfidence = 0.34

type acc struct {
	agent       string
	score       float64
	covered     map[string]struct{}
	coveredList []string
	skills      map[string]struct{}
	skillList   []string
	tagScores   map[string]float64
}

// Match ranks agents covering the given canonical tags. topN limits returned
// candidates (<=0 means all). pinAgent, if non-empty, forces that agent as
// chosen (still computing its coverage). reg must be non-nil.
func Match(tags []string, pinAgent string, topN int, reg *registry.Registry) Result {
	var res Result

	accs := make(map[string]*acc)
	coveredAny := make(map[string]struct{})

	for _, t := range tags {
		tm, tagOK := reg.Tags[t]
		// Skip unknown or inactive tags: an inactive tag must not contribute to
		// any agent's score (mirrors the IsActive check applied to agents below).
		if !tagOK || !tm.IsActive {
			continue
		}
		tagWeight := float64(tm.Weight)
		for _, p := range reg.Postings[t] {
			am, ok := reg.Agents[p.Agent]
			if !ok || !am.IsActive {
				continue
			}
			a := accs[p.Agent]
			if a == nil {
				a = &acc{
					agent:     p.Agent,
					covered:   make(map[string]struct{}),
					skills:    make(map[string]struct{}),
					tagScores: make(map[string]float64),
				}
				accs[p.Agent] = a
			}
			delta := tagWeight * float64(p.SkillWeight)
			a.score += delta
			a.tagScores[t] += delta
			if _, dup := a.covered[t]; !dup {
				a.covered[t] = struct{}{}
				a.coveredList = append(a.coveredList, t)
			}
			if _, dup := a.skills[p.Skill]; !dup {
				a.skills[p.Skill] = struct{}{}
				a.skillList = append(a.skillList, p.Skill)
			}
			coveredAny[t] = struct{}{}
		}
	}

	// Build candidate slice.
	cands := make([]Candidate, 0, len(accs))
	for _, a := range accs {
		sort.Strings(a.coveredList)
		sort.Strings(a.skillList)
		cands = append(cands, Candidate{
			Agent:       a.agent,
			Score:       a.score,
			CoveredTags: a.coveredList,
			Skills:      a.skillList,
			TagScores:   a.tagScores,
		})
	}
	// Deterministic sort: score desc, tie-break agent asc.
	sort.Slice(cands, func(i, j int) bool {
		if cands[i].Score != cands[j].Score {
			return cands[i].Score > cands[j].Score
		}
		return cands[i].Agent < cands[j].Agent
	})

	// covered / uncovered relative to requested tags (stable order = input order).
	for _, t := range tags {
		if _, ok := coveredAny[t]; ok {
			res.CoveredTags = append(res.CoveredTags, t)
		} else {
			res.UncoveredTags = append(res.UncoveredTags, t)
		}
	}

	// pin_agent handling.
	if pinAgent != "" {
		res.PinApplied = true
		res.Chosen = pinAgent
		// Move pinned agent's coverage into result confidence; pinned candidate
		// may be absent from cands if it covers nothing -> add a zero candidate.
		found := false
		for _, c := range cands {
			if c.Agent == pinAgent {
				found = true
				res.Confidence = confidenceFor(c.CoveredTags, tags, c.Score)
				break
			}
		}
		if !found {
			cands = append([]Candidate{{Agent: pinAgent, Score: 0}}, cands...)
			res.Confidence = 0
		}
		res.Candidates = limit(cands, topN)
		return res
	}

	if len(cands) == 0 {
		res.NoMatch = true
		res.Confidence = 0
		res.Candidates = nil
		return res
	}

	top := cands[0]
	res.Chosen = top.Agent
	res.Confidence = confidenceFor(top.CoveredTags, tags, top.Score)
	res.Candidates = limit(cands, topN)
	return res
}

func limit(cands []Candidate, topN int) []Candidate {
	if topN > 0 && len(cands) > topN {
		return cands[:topN]
	}
	return cands
}

// confidenceFor = (fraction of covered tags) * normalizedScore, clamped to [0,1].
// normalizedScore = score / maxPossibleScore for the agent's covered tags, where
// the per-tag max is bounded by 2.0 (skill weight ceiling) -> kept simple and
// monotone. We use coverage ratio as the dominant term.
func confidenceFor(covered, all []string, score float64) float64 {
	if len(all) == 0 {
		return 0
	}
	coverRatio := float64(len(covered)) / float64(len(all))
	// Normalize score by an upper reference (tag weight ~1.3 max * skill ~2.0 max
	// per covered tag) to keep confidence in a sane range without overpowering
	// the coverage signal.
	const perTagRef = 2.6
	maxRef := perTagRef * float64(len(all))
	normScore := 0.0
	if maxRef > 0 {
		normScore = score / maxRef
		if normScore > 1 {
			normScore = 1
		}
	}
	// Weighted blend: coverage is primary (70%), normalized score secondary (30%).
	conf := 0.7*coverRatio + 0.3*normScore
	if conf > 1 {
		conf = 1
	}
	return conf
}
