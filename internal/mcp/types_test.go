package mcp

import (
	"encoding/json"
	"testing"
)

func TestSubprocessRequestJSON(t *testing.T) {
	req := SubprocessRequest{
		RequestID: "req-1",
		ToolName:  "test_tool",
		Arguments: map[string]interface{}{"query": "hello"},
	}
	data, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	var decoded SubprocessRequest
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.RequestID != "req-1" || decoded.ToolName != "test_tool" {
		t.Errorf("roundtrip mismatch: %+v", decoded)
	}
}

func TestSubprocessResponseJSON(t *testing.T) {
	resp := SubprocessResponse{
		Success:   true,
		RequestID: "req-1",
		Content:   []ContentItem{{Type: "text", Text: "result"}},
		Error:     &SubprocessError{Code: "ERR", Message: "fail"},
	}
	data, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	var decoded SubprocessResponse
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if !decoded.Success || decoded.RequestID != "req-1" {
		t.Errorf("roundtrip mismatch: %+v", decoded)
	}
	if decoded.Error == nil || decoded.Error.Code != "ERR" {
		t.Error("error not preserved in roundtrip")
	}
}

func TestContentItemJSON(t *testing.T) {
	item := ContentItem{Type: "image", Data: "base64data", MIMEType: "image/png"}
	data, err := json.Marshal(item)
	if err != nil {
		t.Fatal(err)
	}
	var decoded ContentItem
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Type != "image" || decoded.MIMEType != "image/png" {
		t.Errorf("roundtrip mismatch: %+v", decoded)
	}
}
