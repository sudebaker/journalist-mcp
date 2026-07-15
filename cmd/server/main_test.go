package main

import "testing"

func TestContainsParam(t *testing.T) {
	if !containsParam("postgres://localhost?sslmode=disable", "sslmode") {
		t.Error("expected found")
	}
	if containsParam("postgres://localhost", "sslmode") {
		t.Error("expected not found")
	}
}

func TestContainsChar(t *testing.T) {
	if !containsChar("a?b", '?') {
		t.Error("expected found")
	}
	if containsChar("abc", '?') {
		t.Error("expected not found")
	}
}

func TestTruncateClientError(t *testing.T) {
	short := "hello"
	if truncateClientError(short) != short {
		t.Error("short message should not be truncated")
	}
	long := ""
	for i := 0; i < 600; i++ {
		long += "x"
	}
	result := truncateClientError(long)
	if len(result) >= len(long) {
		t.Error("long message should be truncated")
	}
}
