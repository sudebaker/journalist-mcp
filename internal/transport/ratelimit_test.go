package transport

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestRateLimiter_Allow(t *testing.T) {
	rl := NewRateLimiter(10, 20)

	if !rl.Allow("client1") {
		t.Error("Expected first request to be allowed")
	}

	if !rl.Allow("client1") {
		t.Error("Expected second request to be allowed within limit")
	}
}

func TestRateLimiter_ExceedsBurst(t *testing.T) {
	rl := NewRateLimiter(1, 2)

	for i := 0; i < 2; i++ {
		if !rl.Allow("client1") {
			t.Errorf("Expected request %d to be allowed", i+1)
		}
	}

	if rl.Allow("client1") {
		t.Error("Expected third request to be denied (burst exceeded)")
	}
}

func TestRateLimiter_DifferentClients(t *testing.T) {
	rl := NewRateLimiter(1, 1)

	if !rl.Allow("client1") {
		t.Error("Expected request from client1 to be allowed")
	}

	if !rl.Allow("client2") {
		t.Error("Expected request from client2 to be allowed (different client)")
	}
}

func TestRateLimiter_Middleware(t *testing.T) {
	rl := NewRateLimiter(10, 20)

	handler := rl.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest("GET", "/test", nil)
	w := httptest.NewRecorder()

	handler.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Expected status 200, got %d", w.Code)
	}
}

func TestRateLimiter_XFFIgnoredWithoutTrustedProxy(t *testing.T) {
	rl := NewRateLimiter(1, 1)

	handler := rl.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	spoof := func() *httptest.ResponseRecorder {
		req := httptest.NewRequest("GET", "/test", nil)
		req.RemoteAddr = "198.51.100.7:4444"
		req.Header.Set("X-Forwarded-For", "10.0.0.1")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}

	if resp := spoof(); resp.Code != http.StatusOK {
		t.Errorf("Expected first request status 200, got %d", resp.Code)
	}
	if resp := spoof(); resp.Code != http.StatusTooManyRequests {
		t.Error("Expected spoofed XFF to NOT bypass the rate limit (same underlying client)")
	}
}

func TestRateLimiter_XFFHonoredFromTrustedProxy(t *testing.T) {
	rl := NewRateLimiter(1, 1, "127.0.0.1/32")

	handler := rl.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest("GET", "/test", nil)
	req.RemoteAddr = "127.0.0.1:4444"
	req.Header.Set("X-Forwarded-For", "10.0.0.1")
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Errorf("Expected status 200, got %d", w.Code)
	}

	// Different XFF behind the trusted proxy → a different client to the limiter.
	req2 := httptest.NewRequest("GET", "/test", nil)
	req2.RemoteAddr = "127.0.0.1:4444"
	req2.Header.Set("X-Forwarded-For", "10.0.0.2")
	w2 := httptest.NewRecorder()
	handler.ServeHTTP(w2, req2)
	if w2.Code != http.StatusOK {
		t.Error("Expected trusted-proxy XFF to distinguish clients")
	}
}

func TestRateLimiter_MiddlewareExceeds(t *testing.T) {
	rl := NewRateLimiter(1, 1)

	handler := rl.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	var w *httptest.ResponseRecorder
	for i := 0; i < 2; i++ {
		req := httptest.NewRequest("GET", "/test", nil)
		w = httptest.NewRecorder()
		handler.ServeHTTP(w, req)
	}

	if w.Code != http.StatusTooManyRequests {
		t.Errorf("Expected status 429, got %d", w.Code)
	}

	retryAfter := w.Header().Get("Retry-After")
	if retryAfter == "" {
		t.Error("Expected Retry-After header")
	}
}

func TestRateLimiter_Reset(t *testing.T) {
	rl := NewRateLimiter(1, 1)

	rl.Allow("client1")
	if rl.Allow("client1") {
		t.Error("Expected second request to be denied")
	}

	rl.Reset("client1")

	if !rl.Allow("client1") {
		t.Error("Expected request to be allowed after reset")
	}
}

func TestRateLimiter_ResetAll(t *testing.T) {
	rl := NewRateLimiter(1, 1)

	rl.Allow("client1")
	rl.Allow("client2")

	rl.ResetAll()

	if !rl.Allow("client1") {
		t.Error("Expected request from client1 to be allowed after ResetAll")
	}
	if !rl.Allow("client2") {
		t.Error("Expected request from client2 to be allowed after ResetAll")
	}
}

func TestRateLimiter_TokenRefill(t *testing.T) {
	rl := NewRateLimiter(10, 1)

	if !rl.Allow("client1") {
		t.Error("Expected first request to be allowed")
	}

	if rl.Allow("client1") {
		t.Error("Expected second request to be denied (burst=1)")
	}

	time.Sleep(100 * time.Millisecond)

	if !rl.Allow("client1") {
		t.Error("Expected request to be allowed after token refill")
	}
}
