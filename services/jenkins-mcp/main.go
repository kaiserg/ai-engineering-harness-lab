// Lecturas de Jenkins por MCP para fixtures y para un controlador local.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"regexp"
	"slices"
	"strconv"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type identity struct {
	Workspace string   `json:"workspace_id"`
	Roles     []string `json:"roles"`
	Jobs      []string `json:"jobs"`
}

type arguments struct {
	Job   string
	Build int64
}

var jobPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_-]{0,63}$`)

var inputSchema = map[string]any{
	"type": "object",
	"properties": map[string]any{
		"job":          map[string]any{"type": "string", "pattern": jobPattern.String()},
		"build_number": map[string]any{"type": "integer", "minimum": 1, "maximum": 1000000},
	},
	"required": []string{"job", "build_number"}, "additionalProperties": false,
}

func parseArguments(raw json.RawMessage) (arguments, error) {
	var values map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&values); err != nil || len(values) != 2 {
		return arguments{}, fmt.Errorf("INVALID_ARGUMENT")
	}
	job, okJob := values["job"].(string)
	number, okNumber := values["build_number"].(json.Number)
	build, err := strconv.ParseInt(string(number), 10, 64)
	if !okJob || !okNumber || err != nil || !jobPattern.MatchString(job) || build < 1 || build > 1000000 {
		return arguments{}, fmt.Errorf("INVALID_ARGUMENT")
	}
	return arguments{job, build}, nil
}

func toolError(code string) *mcp.CallToolResult {
	return &mcp.CallToolResult{IsError: true,
		Content:           []mcp.Content{&mcp.TextContent{Text: code}},
		StructuredContent: map[string]any{"error": map[string]any{"code": code}}}
}

func newServer(subject identity, scenario string, delay time.Duration) *mcp.Server {
	return serverFor(subject, scenario, delay, nil)
}

func serverFor(subject identity, scenario string, delay time.Duration, live *liveJenkins) *mcp.Server {
	server := mcp.NewServer(&mcp.Implementation{Name: "jenkins-read", Version: "1"}, nil)
	server.AddTool(&mcp.Tool{Name: "jenkins_get_build", Description: "Consulta un build autorizado. Solo lectura.", InputSchema: inputSchema},
		func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			args, err := parseArguments(req.Params.Arguments)
			if err != nil {
				return toolError("INVALID_ARGUMENT"), nil
			}
			if !(slices.Contains(subject.Roles, "reader") || slices.Contains(subject.Roles, "operator")) || !slices.Contains(subject.Jobs, args.Job) {
				return toolError("FORBIDDEN"), nil
			}
			if live != nil {
				data, err := live.build(ctx, args)
				if err != nil {
					return toolError(err.Error()), nil
				}
				return buildResult(args, data), nil
			}
			if subject.Workspace != "workspace-alpha" || args.Job != "atlas-tests" || args.Build != 101 {
				return toolError("NOT_FOUND"), nil
			}
			timer := time.NewTimer(delay)
			defer timer.Stop()
			select {
			case <-ctx.Done():
				return toolError("UPSTREAM_UNAVAILABLE"), nil
			case <-timer.C:
			}
			if scenario == "unavailable" {
				return toolError("UPSTREAM_UNAVAILABLE"), nil
			}
			revision, result := "revision-A", "SUCCESS"
			if scenario == "stale" {
				revision = "revision-B"
			}
			if scenario == "failed" {
				result = "FAILURE"
			}
			data := map[string]any{"job": args.Job, "build_number": args.Build, "repository": "demo/atlas", "revision": revision, "result": result}
			return buildResult(args, data), nil
		})
	return server
}

func buildResult(args arguments, data map[string]any) *mcp.CallToolResult {
	payload := map[string]any{"schema_version": 1, "data": data,
		"source_id": fmt.Sprintf("jenkins:%s:%d:%s", args.Job, args.Build, data["revision"])}
	text, _ := json.Marshal(payload)
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(text)}}, StructuredContent: payload}
}

func main() {
	var subject identity
	if json.Unmarshal([]byte(os.Getenv("HARNESS_MCP_IDENTITY")), &subject) != nil {
		log.Fatal("INVALID_CONFIGURATION")
	}
	scenario := os.Getenv("HARNESS_BUILD_SCENARIO")
	var live *liveJenkins
	if scenario == "live" {
		var err error
		live, err = newLiveJenkins(os.Getenv("HARNESS_JENKINS_URL"), os.Getenv("HARNESS_JENKINS_JOB"),
			os.Getenv("HARNESS_JENKINS_USER"), os.Getenv("HARNESS_JENKINS_TOKEN"))
		if err != nil {
			log.Fatal("INVALID_CONFIGURATION")
		}
	} else if !slices.Contains([]string{"matching", "stale", "failed", "unavailable"}, scenario) {
		log.Fatal("INVALID_CONFIGURATION")
	}
	delay, err := strconv.ParseFloat(os.Getenv("HARNESS_MCP_DELAY"), 64)
	if err != nil || math.IsNaN(delay) || math.IsInf(delay, 0) || delay < 0 || delay > 3600 {
		log.Fatal("INVALID_CONFIGURATION")
	}
	if err := serverFor(subject, scenario, time.Duration(delay*float64(time.Second)), live).Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatal("MCP_UNAVAILABLE") // stderr; stdout pertenece al protocolo.
	}
}
