// Package registry manages the in-memory index of routing rules loaded from
// the tag catalog (tags.yaml). It provides fast lock-free lookup for the
// hot-path routing decisions via an inverted index tag -> []AgentPosting.
//
// The index is immutable once built; hot reload is performed by atomically
// swapping an *atomic.Pointer[Registry] (RCU). An invalid tags.yaml never
// replaces the active index.
package registry

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"sort"
	"strings"
	"sync/atomic"
	"time"

	"gopkg.in/yaml.v3"
)

// Registry is the in-memory index built from tags.yaml. Immutable after build.
type Registry struct {
	Version    string                    // sha256(tags.yaml) hex, exposed in /healthz
	LoadedAt   time.Time                 // when this index was built
	AliasCanon map[string]string         // (alias|canon) lower-case -> canon
	Tags       map[string]TagMeta        // canon -> tag metadata
	Postings   map[string][]AgentPosting // canon -> inverted postings (agent + skill)
	Agents     map[string]AgentMeta      // agent -> agent metadata

	// canonOrder / agentOrder give deterministic iteration for read APIs.
	canonOrder []string
	agentOrder []string
}

// TagMeta holds metadata for a single canonical tag.
type TagMeta struct {
	Canon       string
	Domain      string
	Description string
	Weight      float32
	IsActive    bool
}

// AgentPosting is one entry of the inverted index: which agent + skill cover a tag.
type AgentPosting struct {
	Agent       string
	Skill       string
	SkillWeight float32
}

// AgentMeta holds metadata for an agent.
type AgentMeta struct {
	Agent    string
	Model    string
	Zone     string
	Heavy    bool
	IsActive bool
}

// ── YAML schema ───────────────────────────────────────────────────────────

type yamlFile struct {
	Tags   []yamlTag   `yaml:"tags"`
	Agents []yamlAgent `yaml:"agents"`
	Skills []yamlSkill `yaml:"skills"`
}

type yamlTag struct {
	Canon       string   `yaml:"canon"`
	Aliases     []string `yaml:"aliases"`
	Domain      string   `yaml:"domain"`
	Description string   `yaml:"description"`
	Weight      *float32 `yaml:"weight"`
	IsActive    *bool    `yaml:"is_active"`
}

type yamlAgent struct {
	Agent    string `yaml:"agent"`
	Model    string `yaml:"model"`
	Zone     string `yaml:"zone"`
	Heavy    int    `yaml:"heavy"`
	IsActive *bool  `yaml:"is_active"`
}

type yamlSkill struct {
	Skill    string   `yaml:"skill"`
	Agent    string   `yaml:"agent"`
	Tags     []string `yaml:"tags"`
	Weight   *float32 `yaml:"weight"`
	IsActive *bool    `yaml:"is_active"`
}

func boolOr(p *bool, def bool) bool {
	if p == nil {
		return def
	}
	return *p
}

func float32Or(p *float32, def float32) float32 {
	if p == nil {
		return def
	}
	return *p
}

// BuildRegistry parses, validates and builds the in-memory index from raw bytes.
// Version is sha256(data). On any validation error it returns (nil, error) and
// the caller must keep the previously active index.
func BuildRegistry(data []byte) (*Registry, error) {
	var f yamlFile
	if err := yaml.Unmarshal(data, &f); err != nil {
		return nil, fmt.Errorf("registry: parse yaml: %w", err)
	}

	sum := sha256.Sum256(data)
	version := hex.EncodeToString(sum[:])

	reg := &Registry{
		Version:    version,
		LoadedAt:   time.Now().UTC(),
		AliasCanon: make(map[string]string),
		Tags:       make(map[string]TagMeta, len(f.Tags)),
		Postings:   make(map[string][]AgentPosting),
		Agents:     make(map[string]AgentMeta, len(f.Agents)),
	}

	// ── tags: canons, aliases, dup detection ──
	for _, t := range f.Tags {
		canon := strings.ToLower(strings.TrimSpace(t.Canon))
		if canon == "" {
			return nil, fmt.Errorf("registry: empty canon in tags")
		}
		if _, dup := reg.Tags[canon]; dup {
			return nil, fmt.Errorf("registry: duplicate canon %q", canon)
		}
		reg.Tags[canon] = TagMeta{
			Canon:       canon,
			Domain:      t.Domain,
			Description: t.Description,
			Weight:      float32Or(t.Weight, 1.0),
			IsActive:    boolOr(t.IsActive, true),
		}
		// alias map: canon -> canon, alias -> canon
		if existing, ok := reg.AliasCanon[canon]; ok && existing != canon {
			return nil, fmt.Errorf("registry: alias/canon collision on %q", canon)
		}
		reg.AliasCanon[canon] = canon
		for _, a := range t.Aliases {
			al := strings.ToLower(strings.TrimSpace(a))
			if al == "" {
				continue
			}
			if existing, ok := reg.AliasCanon[al]; ok && existing != canon {
				return nil, fmt.Errorf("registry: duplicate alias %q (maps to %q and %q)", al, existing, canon)
			}
			reg.AliasCanon[al] = canon
		}
	}

	// ── agents: dup detection ──
	for _, a := range f.Agents {
		name := strings.TrimSpace(a.Agent)
		if name == "" {
			return nil, fmt.Errorf("registry: empty agent name")
		}
		if _, dup := reg.Agents[name]; dup {
			return nil, fmt.Errorf("registry: duplicate agent %q", name)
		}
		reg.Agents[name] = AgentMeta{
			Agent:    name,
			Model:    a.Model,
			Zone:     a.Zone,
			Heavy:    a.Heavy != 0,
			IsActive: boolOr(a.IsActive, true),
		}
	}

	// ── skills: build inverted postings, validate refs ──
	for _, s := range f.Skills {
		if !boolOr(s.IsActive, true) {
			continue
		}
		am, ok := reg.Agents[s.Agent]
		if !ok {
			return nil, fmt.Errorf("registry: skill %q references unknown agent %q", s.Skill, s.Agent)
		}
		// Do not index skills of inactive agents: they can never be chosen, so
		// keeping them only bloats the inverted index (Match also filters them).
		if !am.IsActive {
			continue
		}
		sw := float32Or(s.Weight, 1.0)
		for _, raw := range s.Tags {
			canon := strings.ToLower(strings.TrimSpace(raw))
			if _, ok := reg.Tags[canon]; !ok {
				return nil, fmt.Errorf("registry: skill %q references tag %q absent from registry", s.Skill, raw)
			}
			reg.Postings[canon] = append(reg.Postings[canon], AgentPosting{
				Agent:       s.Agent,
				Skill:       s.Skill,
				SkillWeight: sw,
			})
		}
	}

	// ── deterministic ordering of postings: (agent asc, skill asc) ──
	for canon := range reg.Postings {
		ps := reg.Postings[canon]
		sort.Slice(ps, func(i, j int) bool {
			if ps[i].Agent != ps[j].Agent {
				return ps[i].Agent < ps[j].Agent
			}
			return ps[i].Skill < ps[j].Skill
		})
		reg.Postings[canon] = ps
	}

	// ── deterministic order slices for read APIs ──
	reg.canonOrder = make([]string, 0, len(reg.Tags))
	for c := range reg.Tags {
		reg.canonOrder = append(reg.canonOrder, c)
	}
	sort.Strings(reg.canonOrder)

	reg.agentOrder = make([]string, 0, len(reg.Agents))
	for a := range reg.Agents {
		reg.agentOrder = append(reg.agentOrder, a)
	}
	sort.Strings(reg.agentOrder)

	return reg, nil
}

// LoadFromFile reads the file at path and builds a Registry from it.
func LoadFromFile(path string) (*Registry, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("registry: read %s: %w", path, err)
	}
	return BuildRegistry(data)
}

// Reload performs a full RCU reload cycle: read file, build, atomic swap.
// If the new file's sha256 equals the active version, it is a no-op.
// If validation fails, the active index is left untouched and the error returned.
func Reload(path string, ptr *atomic.Pointer[Registry]) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("registry: read %s: %w", path, err)
	}
	sum := sha256.Sum256(data)
	version := hex.EncodeToString(sum[:])

	if cur := ptr.Load(); cur != nil && cur.Version == version {
		return nil // no-op: same file
	}

	newReg, err := BuildRegistry(data)
	if err != nil {
		return err // active index untouched
	}
	ptr.Store(newReg)
	return nil
}

// CanonOrder returns canonical tag names in deterministic (sorted) order.
func (r *Registry) CanonOrder() []string { return r.canonOrder }

// AgentOrder returns agent names in deterministic (sorted) order.
func (r *Registry) AgentOrder() []string { return r.agentOrder }

// Normalize maps a raw tag (alias or canon, any case) to its canonical name.
// Returns ("", false) if the tag is unknown.
func (r *Registry) Normalize(raw string) (string, bool) {
	canon, ok := r.AliasCanon[strings.ToLower(strings.TrimSpace(raw))]
	return canon, ok
}
