package orchestrator

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/rs/zerolog/log"
	"github.com/sudebaker/journalist-mcp/internal/config"
	"github.com/sudebaker/journalist-mcp/internal/executor"
	"github.com/sudebaker/journalist-mcp/internal/metrics"
	"golang.org/x/sync/errgroup"
)

type InvestigateRequest struct {
	Target     string   `json:"target"`
	TargetType string   `json:"target_type"`
	Sources    []string `json:"sources"`
	DateFrom   string   `json:"date_from"`
	DateTo     string   `json:"date_to"`
}

type SourceResult struct {
	Source           string                 `json:"source"`
	Success          bool                   `json:"success"`
	Error            string                 `json:"error,omitempty"`
	ErrorCode        string                 `json:"error_code,omitempty"`
	Count            int                    `json:"count"`
	DurationMS       int64                  `json:"duration_ms"`
	Official         bool                   `json:"official"`
	DuplicatesRemoved int                   `json:"duplicates_removed,omitempty"`
	Data             map[string]interface{} `json:"data,omitempty"`
}

type InvestigateResult struct {
	Target        string         `json:"target"`
	TargetType    string         `json:"target_type"`
	SourceResults []SourceResult `json:"sources"`
	TotalResults  int            `json:"total_results"`
	DurationMS    int64          `json:"duration_ms,omitempty"`
}

type Orchestrator struct {
	cfg  *config.Config
	exec *executor.Executor
}

func New(cfg *config.Config, exec *executor.Executor) *Orchestrator {
	return &Orchestrator{cfg: cfg, exec: exec}
}

// resolveSources returns the sources to investigate. Explicitly requested
// sources pass through unchanged. When none are requested, it derives the
// sources dynamically from the configured tools, keeping every tool whose
// name ends in "_search" and excluding the orchestrator itself.
func (o *Orchestrator) resolveSources(requested []string) []string {
	if len(requested) > 0 {
		return requested
	}
	var out []string
	for _, t := range o.cfg.Tools {
		if strings.HasSuffix(t.Name, "_search") && t.Name != "journalist_investigate" {
			out = append(out, t.Name)
		}
	}
	return out
}

// resultsCount extracts the number of records reported by a source. The
// preferred signal is the "count" field of the structured_content envelope;
// when absent it falls back to the length of "results" (or legacy "evidence").
func resultsCount(data map[string]interface{}) int {
	if data == nil {
		return 0
	}
	switch v := data["count"].(type) {
	case float64:
		return int(v)
	case int:
		return v
	case int64:
		return int(v)
	}
	for _, key := range []string{"results", "evidence"} {
		if list, ok := data[key].([]interface{}); ok {
			return len(list)
		}
	}
	return 0
}

// sourceOfficial reports the "official" flag the source declared. It defaults
// to false so unmarked sources (e.g. search engines) are never assumed official.
func sourceOfficial(data map[string]interface{}) bool {
	if data == nil {
		return false
	}
	if v, ok := data["official"].(bool); ok {
		return v
	}
	return false
}

func (o *Orchestrator) Investigate(ctx context.Context, req InvestigateRequest) *InvestigateResult {
	start := time.Now()
	sources := o.resolveSources(req.Sources)

	// Preallocate so results keep the requested/configured order instead of
	// completion order. A failed source never cancels or hides the others.
	results := make([]SourceResult, len(sources))

	var mu sync.Mutex
	g, gctx := errgroup.WithContext(ctx)
	seen := make(map[string]struct{})
	for i, src := range sources {
		i, src := i, src
		if err := gctx.Err(); err != nil {
			break
		}
		if o.cfg.GetToolByName(src) == nil {
			log.Warn().Str("source", src).Msg("source tool not configured, skipping")
			results[i] = SourceResult{Source: src, Error: "source tool not configured"}
			continue
		}
		g.Go(func() error {
			args := map[string]interface{}{
				"target":      req.Target,
				"target_type": req.TargetType,
			}
			if req.DateFrom != "" {
				args["date_from"] = req.DateFrom
			}
			if req.DateTo != "" {
				args["date_to"] = req.DateTo
			}
			srcStart := time.Now()
			execResult, err := o.exec.Execute(gctx, src, args)
			sr := SourceResult{Source: src, DurationMS: time.Since(srcStart).Milliseconds()}
			if err != nil {
				sr.Error = err.Error()
				sr.ErrorCode = "EXECUTION_FAILED"
			} else if execResult != nil {
				sr.Success = execResult.Success
				if !execResult.Success && execResult.Error != nil {
					sr.Error = execResult.Error.Message
					sr.ErrorCode = execResult.Error.Code
				}
				sr.Data = execResult.StructuredContent
				sr.Count = resultsCount(execResult.StructuredContent)
				sr.Official = sourceOfficial(execResult.StructuredContent)
				if sr.Success {
					sr.Count, sr.DuplicatesRemoved = countUnique(execResult.StructuredContent, &seen)
				}
			}
			mu.Lock()
			results[i] = sr
			mu.Unlock()
			metrics.RecordSourceExecution(
				sr.Source, sr.Success, sr.ErrorCode,
				sr.Count, sr.DuplicatesRemoved,
				float64(sr.DurationMS)/1000.0,
			)
			return nil
		})
	}
	_ = g.Wait()

	total := 0
	for _, sr := range results {
		if sr.Success {
			total += sr.Count
		}
	}

	return &InvestigateResult{
		Target:        req.Target,
		TargetType:    req.TargetType,
		SourceResults: results,
		TotalResults:  total,
		DurationMS:    time.Since(start).Milliseconds(),
	}
}

// countUnique deduplicates evidence by deterministic id across sources.
// Records whose id was already contributed by an earlier source are counted
// in duplicatesRemoved instead of inflating the total.
func countUnique(data map[string]interface{}, seen *map[string]struct{}) (int, int) {
	if data == nil {
		return 0, 0
	}
	var items []interface{}
	switch v := data["results"].(type) {
	case []interface{}:
		items = v
	case []map[string]interface{}:
		for _, m := range v {
			items = append(items, m)
		}
	default:
		if legacy, ok := data["evidence"].([]interface{}); ok {
			items = legacy
		}
	}
	count := 0
	removed := 0
	for _, it := range items {
		id, _ := itID(it)
		if id == "" {
			count++
			continue
		}
		if _, exists := (*seen)[id]; exists {
			removed++
			continue
		}
		(*seen)[id] = struct{}{}
		count++
	}
	return count, removed
}

// itID extracts the deterministic "id" field from either a map or a map+JSON
// byte slice produced by the executor.
func itID(it interface{}) (string, bool) {
	if m, ok := it.(map[string]interface{}); ok {
		if id, ok := m["id"].(string); ok {
			return id, true
		}
	}
	return "", false
}