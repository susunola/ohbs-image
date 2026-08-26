package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

type Client struct {
	Endpoint, Token string
	HTTPClient      *http.Client
}
type channelResponse struct {
	Channel struct {
		Name       string `json:"channel"`
		Generation int64  `json:"generation"`
	} `json:"channel"`
	Artifact struct {
		ID      string `json:"artifact_id"`
		Bucket  string `json:"bucket"`
		Version string `json:"version"`
		Region  string `json:"region"`
		Status  string `json:"status"`
	} `json:"artifact"`
}

func (client *Client) Resolve(ctx context.Context, bucket, channel string) (*channelResponse, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet,
		client.Endpoint+"/api/v1/channels/"+url.PathEscape(bucket)+"/"+url.PathEscape(channel), nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+client.Token)
	httpClient := client.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 15 * time.Second}
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("control plane returned %s", response.Status)
	}
	var resolved channelResponse
	if err := json.NewDecoder(response.Body).Decode(&resolved); err != nil {
		return nil, err
	}
	if resolved.Artifact.Status != "active" || resolved.Artifact.ID == "" {
		return nil, fmt.Errorf("resolved artifact is missing or inactive")
	}
	return &resolved, nil
}
