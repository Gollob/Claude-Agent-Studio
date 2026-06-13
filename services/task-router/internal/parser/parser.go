// Package parser is the deterministic entry point of the service. It extracts
// tags (#tag, @agent, [domain]) from task text WITHOUT calling any LLM,
// normalizes them to canonical tags via the registry, decides fast/slow path,
// and computes a stable hash of the normalized text for the triage cache.
package parser

import (
	"crypto/sha256"
	"encoding/hex"
	"regexp"
	"strings"

	"task-router/internal/registry"
)

var (
	reTag    = regexp.MustCompile(`#([\p{L}\p{N}_.\-]+)`)
	reAgent  = regexp.MustCompile(`@([\p{L}\p{N}_\-]+)`)
	reDomain = regexp.MustCompile(`\[([\p{L}\p{N}_.\-]+)\]`)
	reSpace  = regexp.MustCompile(`\s+`)
	rePunct  = regexp.MustCompile(`[^\p{L}\p{N}\s]+`)
)

// Result is the outcome of parsing a single task.
type Result struct {
	CanonTags    []string // canonical, deduplicated, registry-known tags
	UnknownTags  []string // extracted tags absent from the registry
	PinAgent     string   // explicit @agent, validated against registry (empty if none/unknown)
	Fast         bool     // true => fast-path (matchable without LLM)
	TextHash     string   // sha256 of normalized text (triage cache key)
	RawExtracted []string // every token extracted from text+tags before normalization (debug)
}

// Parse extracts and normalizes tags for one task.
//
//	text     — raw task text (may contain #tag @agent [domain])
//	extraTags — tags passed as a separate request field (merged + deduped)
//	reg      — active registry snapshot (alias->canon)
func Parse(text string, extraTags []string, reg *registry.Registry) Result {
	var res Result

	// 1. Extract raw tokens from text.
	raw := make([]string, 0, 8)
	for _, m := range reTag.FindAllStringSubmatch(text, -1) {
		raw = append(raw, m[1])
	}
	for _, m := range reDomain.FindAllStringSubmatch(text, -1) {
		raw = append(raw, m[1])
	}
	// Explicit agent pin.
	if am := reAgent.FindStringSubmatch(text); am != nil {
		cand := strings.TrimSpace(am[1])
		if reg != nil {
			if meta, ok := reg.Agents[cand]; ok && meta.IsActive {
				res.PinAgent = cand
			}
		}
	}
	// Tags passed as a separate field.
	raw = append(raw, extraTags...)
	res.RawExtracted = append([]string(nil), raw...)

	// 2. Normalize alias -> canon, split known/unknown, dedup.
	seenCanon := make(map[string]struct{})
	seenUnknown := make(map[string]struct{})
	for _, r := range raw {
		key := strings.ToLower(strings.TrimSpace(r))
		if key == "" {
			continue
		}
		if reg != nil {
			if canon, ok := reg.Normalize(key); ok {
				if _, dup := seenCanon[canon]; !dup {
					seenCanon[canon] = struct{}{}
					res.CanonTags = append(res.CanonTags, canon)
				}
				continue
			}
		}
		if _, dup := seenUnknown[key]; !dup {
			seenUnknown[key] = struct{}{}
			res.UnknownTags = append(res.UnknownTags, key)
		}
	}

	// 3. Decide fast vs slow path.
	//    fast if at least one canonical tag OR an explicit valid pin_agent.
	res.Fast = len(res.CanonTags) > 0 || res.PinAgent != ""

	// 4. Stable hash of the normalized text for the triage cache.
	res.TextHash = HashText(text)

	return res
}

// NormalizeText lower-cases, strips punctuation and collapses whitespace so that
// minor wording variations map to the same hash.
func NormalizeText(text string) string {
	t := strings.ToLower(text)
	t = rePunct.ReplaceAllString(t, " ")
	t = reSpace.ReplaceAllString(t, " ")
	return strings.TrimSpace(t)
}

// HashText returns sha256 hex of the normalized text.
func HashText(text string) string {
	sum := sha256.Sum256([]byte(NormalizeText(text)))
	return hex.EncodeToString(sum[:])
}

// NormalizeTags maps an arbitrary list of raw tags to canonical tags via the
// registry, dropping unknowns and deduping. Used by the triage callback path.
func NormalizeTags(raw []string, reg *registry.Registry) (canon, unknown []string) {
	seenC := make(map[string]struct{})
	seenU := make(map[string]struct{})
	for _, r := range raw {
		key := strings.ToLower(strings.TrimSpace(r))
		if key == "" {
			continue
		}
		if reg != nil {
			if c, ok := reg.Normalize(key); ok {
				if _, dup := seenC[c]; !dup {
					seenC[c] = struct{}{}
					canon = append(canon, c)
				}
				continue
			}
		}
		if _, dup := seenU[key]; !dup {
			seenU[key] = struct{}{}
			unknown = append(unknown, key)
		}
	}
	return canon, unknown
}
