package session

import "testing"

func TestStoreSetGetDelete(t *testing.T) {
	s := New()
	s.Set("s1", "user-1")
	val, ok := s.Get("s1")
	if !ok || val != "user-1" {
		t.Errorf("expected user-1, got %q (found=%v)", val, ok)
	}
	s.Delete("s1")
	_, ok = s.Get("s1")
	if ok {
		t.Error("expected session deleted")
	}
}

func TestStoreGetNotFound(t *testing.T) {
	s := New()
	_, ok := s.Get("nonexistent")
	if ok {
		t.Error("expected not found")
	}
}

func TestStoreGetAll(t *testing.T) {
	s := New()
	s.Set("a", "u1")
	s.Set("b", "u2")
	all := s.GetAll()
	if len(all) != 2 {
		t.Errorf("expected 2 entries, got %d", len(all))
	}
	if all["a"] != "u1" || all["b"] != "u2" {
		t.Errorf("unexpected entries: %v", all)
	}
}
