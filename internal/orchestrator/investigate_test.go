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

func TestResolveSourcesFiltersSearchTools(t *testing.T) {
	cfg := &config.Config{
		Tools: []config.ToolConfig{
			{Name: "ted_search"},
			{Name: "borme_search"},
			{Name: "boe_search"},
			{Name: "bdns_search"},
			{Name: "contratacion_search"},
			{Name: "doue_search"},
			{Name: "transparency_search"},
			{Name: "searxng_search"},
			{Name: "journalist_investigate"},
			{Name: "datetime"},
			{Name: "rustfs_storage"},
		},
	}
	inv := New(cfg, nil)
	sources := inv.resolveSources(nil)
	expected := []string{
		"ted_search", "borme_search", "boe_search", "bdns_search",
		"contratacion_search", "doue_search", "transparency_search", "searxng_search",
	}
	if len(sources) != len(expected) {
		t.Fatalf("expected %d search sources, got %d: %v", len(expected), len(sources), sources)
	}
	for i, s := range expected {
		if sources[i] != s {
			t.Errorf("sources[%d] = %q, want %q (full: %v)", i, sources[i], s, sources)
		}
	}
}

func TestResolveSourcesKeepsExplicitRequest(t *testing.T) {
	cfg := &config.Config{
		Tools: []config.ToolConfig{{Name: "ted_search"}, {Name: "boe_search"}},
	}
	inv := New(cfg, nil)
	requested := []string{"boe_search", "custom_thing"}
	sources := inv.resolveSources(requested)
	if len(sources) != 2 {
		t.Fatalf("expected 2 sources (requested passthrough), got %d: %v", len(sources), sources)
	}
	if sources[0] != "boe_search" || sources[1] != "custom_thing" {
		t.Errorf("resolveSources should return requested unchanged, got %v", sources)
	}
}

