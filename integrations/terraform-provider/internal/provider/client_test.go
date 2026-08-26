package provider

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
)

type roundTrip func(*http.Request) (*http.Response, error)

func (function roundTrip) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestResolveRequiresBearerAndActiveArtifact(t *testing.T) {
	transport := roundTrip(func(request *http.Request) (*http.Response, error) {
		if request.Header.Get("Authorization") != "Bearer test-token" {
			t.Fatalf("authorization header was not forwarded")
		}
		return &http.Response{StatusCode: 200, Status: "200 OK", Body: io.NopCloser(strings.NewReader(
			`{"channel":{"channel":"stable","generation":4},` +
				`"artifact":{"artifact_id":"img-1","bucket":"rhel10","version":"1","region":"ap-guangzhou","status":"active"}}`))}, nil
	})
	client := &Client{Endpoint: "https://images.example", Token: "test-token",
		HTTPClient: &http.Client{Transport: transport}}
	result, err := client.Resolve(context.Background(), "rhel10", "stable")
	if err != nil || result.Artifact.ID != "img-1" || result.Channel.Generation != 4 {
		t.Fatalf("unexpected resolution: %#v, %v", result, err)
	}
}

func TestResolveFailsClosedForInactiveArtifact(t *testing.T) {
	transport := roundTrip(func(_ *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Status: "200 OK", Body: io.NopCloser(strings.NewReader(
			`{"channel":{"channel":"stable","generation":4},` +
				`"artifact":{"artifact_id":"img-1","status":"revoked"}}`))}, nil
	})
	_, err := (&Client{Endpoint: "https://images.example", Token: "test-token",
		HTTPClient: &http.Client{Transport: transport}}).Resolve(
		context.Background(), "rhel10", "stable")
	if err == nil {
		t.Fatal("inactive artifact must be rejected")
	}
}
