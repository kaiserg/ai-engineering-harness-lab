package main

import (
	"encoding/json"
	"testing"
)

func TestArgumentsRejectUntrustedFieldsAndTypes(t *testing.T) {
	for _, raw := range []string{
		`null`, `{}`, `{"job":"../atlas","build_number":101}`,
		`{"job":"https://example.com","build_number":101}`,
		`{"job":"atlas-tests","build_number":true}`,
		`{"job":"atlas-tests","build_number":0}`,
		`{"job":"atlas-tests","build_number":1000001}`,
		`{"job":"atlas-tests","build_number":101,"role":"admin"}`,
	} {
		t.Run(raw, func(t *testing.T) {
			if _, err := parseArguments(json.RawMessage(raw)); err == nil {
				t.Fatal("expected INVALID_ARGUMENT")
			}
		})
	}
}

func TestArgumentsPreserveResource(t *testing.T) {
	args, err := parseArguments(json.RawMessage(`{"job":"atlas-tests","build_number":101}`))
	if err != nil || args.Job != "atlas-tests" || args.Build != 101 {
		t.Fatalf("unexpected parse: %+v %v", args, err)
	}
}
