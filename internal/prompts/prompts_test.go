package prompts

import (
	"testing"

	"github.com/sudebaker/journalist-mcp/internal/config"
)

func TestInterpolateTemplate(t *testing.T) {
	tests := []struct {
		name     string
		tmpl     string
		args     map[string]string
		expected string
	}{
		{"no placeholders", "hello", map[string]string{}, "hello"},
		{"single", "Research {{topic}}", map[string]string{"topic": "AI"}, "Research AI"},
		{"multiple", "{{a}} and {{b}}", map[string]string{"a": "X", "b": "Y"}, "X and Y"},
		{"unmatched", "Hello {{name}}", map[string]string{}, "Hello {{name}}"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := interpolateTemplate(tt.tmpl, tt.args)
			if got != tt.expected {
				t.Errorf("got %q, want %q", got, tt.expected)
			}
		})
	}
}

func TestCreatePromptHandler(t *testing.T) {
	cfg := config.PromptConfig{
		Name:        "test",
		Description: "Test prompt",
		Arguments: []config.PromptArgumentConfig{
			{Name: "topic", Description: "Topic", Required: true},
		},
		Messages: []config.PromptMessageConfig{
			{Role: "user", Content: "Research {{topic}}"},
		},
	}
	handler := createPromptHandler(cfg)
	if handler == nil {
		t.Fatal("expected non-nil handler")
	}
}
