package config

import (
	"fmt"
	"os"
	"strings"
)

// insecureSecretValues lists well-known development credentials that must never
// appear in a configured execution environment. Detection runs after env
// expansion, so a value that survived a "${VAR:-insecure-default}" fallback in
// the YAML is caught at startup instead of silently deployed.
var insecureSecretValues = []string{
	"mcppassword",
	"rustfsadmin",
	"crawl4ai-dev-token-abc123def456",
}

// requiredSecrets are environment variables that must hold a non-empty value
// when MCP_REQUIRE_SECRETS is truthy. Enable in production deployments; local
// development can skip the strict check.
var requiredSecrets = []string{
	"DATABASE_URL",
	"RUSTFS_ACCESS_KEY_ID",
	"RUSTFS_SECRET_ACCESS_KEY",
	"SEARXNG_SECRET",
}

func isTruthyValue(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

func maskSecret(v string) string {
	if len(v) <= 4 {
		return "***"
	}
	return v[:2] + "***" + v[len(v)-2:]
}

// ValidateSecrets inspects the already-expanded execution environment for
// insecure default credentials. It always rejects known dev credentials, and
// additionally requires requiredSecrets to be non-empty when the
// MCP_REQUIRE_SECRETS environment variable is set to a truthy value.
func ValidateSecrets(env map[string]string) error {
	var offenders []string
	for name, value := range env {
		lower := strings.ToLower(value)
		for _, bad := range insecureSecretValues {
			if strings.Contains(lower, bad) {
				offenders = append(offenders, fmt.Sprintf("%s=%s", name, maskSecret(value)))
				break
			}
		}
	}
	if len(offenders) > 0 {
		return fmt.Errorf("insecure default credentials detected in execution.environment: %s", strings.Join(offenders, ", "))
	}

	if isTruthyValue(os.Getenv("MCP_REQUIRE_SECRETS")) {
		var missing []string
		for _, name := range requiredSecrets {
			if strings.TrimSpace(env[name]) == "" {
				missing = append(missing, name)
			}
		}
		if len(missing) > 0 {
			return fmt.Errorf("required secrets missing from execution.environment: %s", strings.Join(missing, ", "))
		}
	}
	return nil
}