package tracing

import (
	"context"
	"errors"
	"testing"
)

func TestNoOpTracer(t *testing.T) {
	tracer := NoOpTracer()
	if tracer == nil {
		t.Fatal("NoOpTracer returned nil")
	}
	span, _ := tracer.StartSpan(context.Background(), "op")
	if span != nil {
		t.Error("expected nil span from NoOpTracer")
	}
}

func TestTracerStartSpan(t *testing.T) {
	tracer := NewTracer("test")
	span, ctx := tracer.StartSpan(context.Background(), "op")
	if span == nil {
		t.Fatal("expected non-nil span")
	}
	if span.TraceID == "" {
		t.Error("expected non-empty TraceID")
	}
	if span.SpanID == "" {
		t.Error("expected non-empty SpanID")
	}
	if ctx == nil {
		t.Error("expected non-nil context")
	}
	span.End()
}

func TestTracerSpanContextPropagation(t *testing.T) {
	tracer := NewTracer("test")
	span1, ctx1 := tracer.StartSpan(context.Background(), "op1")
	span2, _ := tracer.StartSpan(ctx1, "op2")
	if span2.TraceID != span1.TraceID {
		t.Errorf("TraceID mismatch: %s vs %s", span2.TraceID, span1.TraceID)
	}
	span1.End()
	span2.End()
}

func TestSpanSetAttribute(t *testing.T) {
	tracer := NewTracer("test")
	span, _ := tracer.StartSpan(context.Background(), "op")
	span.SetAttribute("key", "value")
	span.SetAttribute("count", 42)
	span.End()
}

func TestSpanRecordError(t *testing.T) {
	tracer := NewTracer("test")
	span, _ := tracer.StartSpan(context.Background(), "op")
	span.RecordError(errors.New("test error"))
	span.RecordError(nil) // should not panic
	span.End()
}

func TestSpanNilSafety(t *testing.T) {
	var s *Span
	s.SetAttribute("k", "v")
	s.RecordError(errors.New("e"))
	s.End()
}
