package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestLiveReadsOnlyConfiguredJobAndArchivedRevision(t *testing.T) {
	sha := "7044a8a032e85b6ab611033b2ac8af7ce85805b2"
	calls := make(chan string, 4)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls <- r.Method + " " + r.URL.Path
		user, token, _ := r.BasicAuth()
		if user != "reader" || token != "test-token" {
			t.Error("missing scoped credentials")
			w.WriteHeader(403)
			return
		}
		switch r.URL.Path {
		case "/job/atlas-tests/1/api/json":
			fmt.Fprint(w, `{"number":1,"building":false,"result":"SUCCESS","artifacts":[{"relativePath":"harness-revision.json"}]}`)
		case "/job/atlas-tests/1/artifact/harness-revision.json":
			fmt.Fprintf(w, `{"repository":"octocat/hello-world","pull_number":1,"revision":"%s"}`, sha)
		default:
			t.Error("unexpected path")
			w.WriteHeader(404)
		}
	}))
	defer stub.Close()
	j, err := newLiveJenkins(stub.URL, "atlas-tests", "reader", "test-token")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := j.build(context.Background(), arguments{"other", 1}); err == nil || err.Error() != "FORBIDDEN" {
		t.Fatal("unexpected authorization result", err)
	}
	got, err := j.build(context.Background(), arguments{"atlas-tests", 1})
	if err != nil || got["revision"] != sha {
		t.Fatalf("unexpected build: %v %v", got, err)
	}
	for _, want := range []string{"GET /job/atlas-tests/1/api/json", "GET /job/atlas-tests/1/artifact/harness-revision.json"} {
		if got := <-calls; got != want {
			t.Errorf("request = %q; want %q", got, want)
		}
	}
}

func TestLiveDoesNotFollowRedirects(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("redirect was followed") }))
	defer target.Close()
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target.URL, http.StatusFound) }))
	defer stub.Close()
	j, err := newLiveJenkins(stub.URL, "atlas-tests", "reader", "test-token")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := j.build(context.Background(), arguments{"atlas-tests", 1}); err == nil || err.Error() != "UPSTREAM_UNAVAILABLE" {
		t.Fatal("unexpected redirect result", err)
	}
}
