package orchestrator

import (
	"context"
	"sync"

	"github.com/rs/zerolog/log"
	"github.com/sudebaker/journalist-mcp/internal/config"
	"github.com/sudebaker/journalist-mcp/internal/executor"
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
	Source  string                 `json:"source"`
	Success bool                   `json:"success"`
	Error   string                 `json:"error,omitempty"`
	Data    map[string]interface{} `json:"data,omitempty"`
}

type InvestigateResult struct {
	Target        string         `json:"target"`
	TargetType    string         `json:"target_type"`
	SourceResults []SourceResult `json:"sources"`
	TotalResults  int            `json:"total_results"`
}

type Orchestrator struct {
	cfg  *config.Config
	exec *executor.Executor
}

func New(cfg *config.Config, exec *executor.Executor) *Orchestrator {
	return &Orchestrator{cfg: cfg, exec: exec}
}

var defaultSources = []string{
	"ted_search", "borme_search", "boe_search", "searxng_search",
}

func (o *Orchestrator) Investigate(ctx context.Context, req InvestigateRequest) *InvestigateResult {
	sources := req.Sources
	if len(sources) == 0 {
		sources = defaultSources
	}

	var mu sync.Mutex
	result := &InvestigateResult{
		Target:        req.Target,
		TargetType:    req.TargetType,
		SourceResults: make([]SourceResult, 0, len(sources)),
	}

	g, gctx := errgroup.WithContext(ctx)
	for _, src := range sources {
		src := src
		if o.cfg.GetToolByName(src) == nil {
			log.Warn().Str("source", src).Msg("source tool not configured, skipping")
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
			execResult, err := o.exec.Execute(gctx, src, args)
			mu.Lock()
			defer mu.Unlock()
			sr := SourceResult{Source: src}
			if err != nil {
				sr.Error = err.Error()
			} else if execResult != nil {
				sr.Success = execResult.Success
				if !execResult.Success && execResult.Error != nil {
					sr.Error = execResult.Error.Message
				}
				sr.Data = execResult.StructuredContent
			}
			result.SourceResults = append(result.SourceResults, sr)
			if sr.Success {
				result.TotalResults++
			}
			return nil
		})
	}
	_ = g.Wait()
	return result
}
