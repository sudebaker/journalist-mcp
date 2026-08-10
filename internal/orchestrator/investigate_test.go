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
	// nonexistent_tool not in config: reported as a failed source with a
	// distinguishable error code instead of being silently dropped.
	if len(result.SourceResults) != 1 {
		t.Fatalf("expected 1 source result, got %d: %v", len(result.SourceResults), result.SourceResults)
	}
	sr := result.SourceResults[0]
	if sr.Source != "nonexistent_tool" {
		t.Errorf("expected source nonexistent_tool, got %q", sr.Source)
	}
	if sr.Success {
		t.Errorf("expected success=false for unconfigured source")
	}
	if sr.Error != "source tool not configured" {
		t.Errorf("expected descriptive error, got %q", sr.Error)
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

func TestResultsCountUsesEnvelopeCount(t *testing.T) {
	if got := resultsCount(map[string]interface{}{"count": 4.0, "results": []interface{}{}}); got != 4 {
		t.Errorf("expected 4 from envelope count, got %d", got)
	}
	if got := resultsCount(map[string]interface{}{"results": []interface{}{1, 2, 3}}); got != 3 {
		t.Errorf("expected 3 from results fallback, got %d", got)
	}
	if got := resultsCount(nil); got != 0 {
		t.Errorf("expected 0 for nil data, got %d", got)
	}
}

func TestSourceOfficialDefaultsFalse(t *testing.T) {
	if sourceOfficial(map[string]interface{}{"official": true}) != true {
		t.Error("expected official=true when declared")
	}
	if sourceOfficial(map[string]interface{}{}) != false {
		t.Error("expected official=false when absent (e.g. searxng)")
	}
	if sourceOfficial(nil) != false {
		t.Error("expected official=false for nil data")
	}
}

func TestCountUniqueDeduplicatesDeterministically(t *testing.T) {
	seen := make(map[string]struct{})
	first := map[string]interface{}{
		"results": []interface{}{
			map[string]interface{}{"id": "aaa"},
			map[string]interface{}{"id": "bbb"},
		},
	}
	second := map[string]interface{}{
		"results": []interface{}{
			map[string]interface{}{"id": "bbb"}, // same record from another source
			map[string]interface{}{"id": "ccc"},
		},
	}
	count, removed := countUnique(first, &seen)
	if count != 2 || removed != 0 {
		t.Errorf("first source: count=%d removed=%d, want 2/0", count, removed)
	}
	count, removed = countUnique(second, &seen)
	if count != 1 || removed != 1 {
		t.Errorf("second source: count=%d removed=%d, want 1/1", count, removed)
	}
}

