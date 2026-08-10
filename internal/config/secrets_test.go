package config

import (
	"os"
	"testing"
)

func TestValidateSecretsAcceptsRealValues(t *testing.T) {
	os.Unsetenv("MCP_REQUIRE_SECRETS")
	env := map[string]string{
		"DATABASE_URL":               "postgresql://app:s3cr3t-pass@db:5432/knowledge",
		"RUSTFS_SECRET_ACCESS_KEY":   "a-very-long-real-secret",
		"CRAWL4AI_TOKEN":             "",
		"MEMGRAPH_PASSWORD":          "",
	}
	if err := ValidateSecrets(env); err != nil {
		t.Fatalf("expected no error for real values, got: %v", err)
	}
}

func TestValidateSecretsRejectsInsecureDefaults(t *testing.T) {
	os.Unsetenv("MCP_REQUIRE_SECRETS")
	cases := []map[string]string{
		{"DATABASE_URL": "postgresql://mcp:mcppassword@postgres:5432/knowledge"},
		{"RUSTFS_ACCESS_KEY_ID": "rustfsadmin"},
		{"RUSTFS_SECRET_ACCESS_KEY": "rustfsadmin"},
		{"CRAWL4AI_TOKEN": "crawl4ai-dev-token-abc123def456"},
	}
	for _, env := range cases {
		if err := ValidateSecrets(env); err == nil {
			t.Errorf("expected error for insecure default, got nil (env=%v)", env)
		}
	}
}

func TestValidateSecretsRequiresSecretsInStrictMode(t *testing.T) {
	os.Setenv("MCP_REQUIRE_SECRETS", "1")
	defer os.Unsetenv("MCP_REQUIRE_SECRETS")

	env := map[string]string{
		"DATABASE_URL":             "postgresql://app:pass@db:5432/knowledge",
		"RUSTFS_ACCESS_KEY_ID":     "access",
		"RUSTFS_SECRET_ACCESS_KEY": "secret",
		// SEARXNG_SECRET intentionally missing
	}
	if err := ValidateSecrets(env); err == nil {
		t.Fatal("expected error for missing SEARXNG_SECRET in strict mode, got nil")
	}

	env["SEARXNG_SECRET"] = "1234567890abcdef"
	if err := ValidateSecrets(env); err != nil {
		t.Fatalf("expected no error with all required secrets set, got: %v", err)
	}
}

func TestMaskSecret(t *testing.T) {
	if got := maskSecret("abcdef"); got != "ab***ef" {
		t.Errorf("expected ab***ef, got %q", got)
	}
	if got := maskSecret("ab"); got != "***" {
		t.Errorf("expected ***, got %q", got)
	}
}