package orchestrator

import (
	"context"
	"testing"

	"github.com/sudebaker/journalist-mcp/internal/config"
)

func TestInvestigateSkipsMissingTools(t *testing.T) {
	cfg := &config.Config{
		Tools: []config.ToolConfig{},
	}
	inv := New(cfg, nil)
	result := inv.Investigate(context.Background(), InvestigateRequest{
		Target:     "B12345678",
		TargetType: "nif",
	})
	if result == nil {
		t.Fatal("expected non-nil result")
	}
	if len(result.SourceResults) != 0 {
		t.Errorf("expected 0 source results (no tools configured), got %d", len(result.SourceResults))
	}
}

func TestInvestigateUsesProvidedSources(t *testing.T) {
	cfg := &config.Config{
		Tools: []config.ToolConfig{
			{Name: "ted_search"},
			{Name: "borme_search"},
		},
	}
	inv := New(cfg, nil)
	result := inv.Investigate(context.Background(), InvestigateRequest{
		Target:     "test",
		TargetType: "name",
		Sources:    []string{"nonexistent_tool"},
	})
	if result == nil {
		t.Fatal("expected non-nil result")
	}
	// nonexistent_tool not in config, so 0 sources
	if len(result.SourceResults) != 0 {
		t.Errorf("expected 0 sources, got %d", len(result.SourceResults))
	}
}
