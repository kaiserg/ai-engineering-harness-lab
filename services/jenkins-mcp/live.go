package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"time"
)

var revisionPattern = regexp.MustCompile("^[0-9a-f]{40}([0-9a-f]{24})?$")

type liveJenkins struct {
	base, job, user, token string
	client                 *http.Client
}

func newLiveJenkins(base, job, user, token string) (*liveJenkins, error) {
	u, err := url.Parse(base)
	if err != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.User != nil ||
		u.Path != "" || u.RawQuery != "" || u.Fragment != "" || u.ForceQuery ||
		!jobPattern.MatchString(job) || user == "" || token == "" {
		return nil, errors.New("INVALID_CONFIGURATION")
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil || port < 1 || port > 65535 {
		return nil, errors.New("INVALID_CONFIGURATION")
	}
	return &liveJenkins{base, job, user, token, &http.Client{
		Timeout:       4 * time.Second,
		Transport:     &http.Transport{DialContext: (&net.Dialer{Timeout: 2 * time.Second}).DialContext},
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
	}}, nil
}

func (j *liveJenkins) read(ctx context.Context, path string, target any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, j.base+path, nil)
	if err != nil {
		return errors.New("INVALID_CONFIGURATION")
	}
	req.SetBasicAuth(j.user, j.token)
	resp, err := j.client.Do(req)
	if err != nil {
		return errors.New("UPSTREAM_UNAVAILABLE")
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case 200:
	case 401, 403:
		return errors.New("FORBIDDEN")
	case 404:
		return errors.New("NOT_FOUND")
	default:
		return errors.New("UPSTREAM_UNAVAILABLE")
	}
	const limit = 32768
	data, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return errors.New("UPSTREAM_UNAVAILABLE")
	}
	if len(data) > limit || json.Unmarshal(data, target) != nil {
		return errors.New("INVALID_TOOL_OUTPUT")
	}
	return nil
}

func (j *liveJenkins) build(ctx context.Context, args arguments) (map[string]any, error) {
	if args.Job != j.job {
		return nil, errors.New("FORBIDDEN")
	}
	path := fmt.Sprintf("/job/%s/%d", args.Job, args.Build)
	var build struct {
		Number    int64
		Building  *bool
		Result    string
		Artifacts []struct{ RelativePath string }
	}
	if err := j.read(ctx, path+"/api/json?tree=number,building,result,artifacts%5BrelativePath%5D", &build); err != nil {
		return nil, err
	}
	if build.Number != args.Build || build.Building == nil {
		return nil, errors.New("INVALID_TOOL_OUTPUT")
	}
	if *build.Building {
		return nil, errors.New("UPSTREAM_UNAVAILABLE")
	}
	if build.Result != "SUCCESS" && build.Result != "FAILURE" {
		return nil, errors.New("UPSTREAM_UNAVAILABLE")
	}
	archived := false
	for _, item := range build.Artifacts {
		if item.RelativePath == "harness-revision.json" {
			archived = true
		}
	}
	if !archived {
		return nil, errors.New("UPSTREAM_UNAVAILABLE")
	}
	var revision struct {
		Repository string
		PullNumber int64 `json:"pull_number"`
		Revision   string
	}
	if err := j.read(ctx, path+"/artifact/harness-revision.json", &revision); err != nil {
		return nil, err
	}
	if revision.Repository == "" || revision.PullNumber < 1 || revision.PullNumber > 1000000 ||
		!revisionPattern.MatchString(revision.Revision) {
		return nil, errors.New("INVALID_TOOL_OUTPUT")
	}
	return map[string]any{"job": args.Job, "build_number": args.Build, "repository": revision.Repository,
		"pull_number": revision.PullNumber, "revision": revision.Revision, "result": build.Result}, nil
}
