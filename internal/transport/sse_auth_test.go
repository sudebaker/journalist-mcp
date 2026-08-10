package transport

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func okHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
}

func TestBearerAuthHandler_MissingKeyPermissive(t *testing.T) {
	h := bearerAuthHandler("", "/mcp")(okHandler())
	req := httptest.NewRequest("POST", "/mcp", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Errorf("expected 200 in permissive mode, got %d", w.Code)
	}
}

func TestBearerAuthHandler_RequiresToken(t *testing.T) {
	h := bearerAuthHandler("s3cr3t-key", "/mcp")(okHandler())

	req := httptest.NewRequest("POST", "/mcp", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 without Authorization, got %d", w.Code)
	}
}

func TestBearerAuthHandler_WrongToken(t *testing.T) {
	h := bearerAuthHandler("s3cr3t-key", "/mcp")(okHandler())

	req := httptest.NewRequest("POST", "/mcp", nil)
	req.Header.Set("Authorization", "Bearer wrong")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 with wrong token, got %d", w.Code)
	}
}

func TestBearerAuthHandler_CorrectToken(t *testing.T) {
	h := bearerAuthHandler("s3cr3t-key", "/mcp")(okHandler())

	req := httptest.NewRequest("POST", "/mcp", nil)
	req.Header.Set("Authorization", "Bearer s3cr3t-key")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Errorf("expected 200 with correct token, got %d", w.Code)
	}
}

func TestBearerAuthHandler_NonBearerScheme(t *testing.T) {
	h := bearerAuthHandler("s3cr3t-key", "/mcp")(okHandler())

	req := httptest.NewRequest("POST", "/mcp", nil)
	req.Header.Set("Authorization", "Basic dXNlcjpwYXNz")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 for non-Bearer scheme, got %d", w.Code)
	}
}