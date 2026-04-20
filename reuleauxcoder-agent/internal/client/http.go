package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

type HTTPClient struct {
	baseURL string
	http    *http.Client
}

func New(baseURL string) *HTTPClient {
	return &HTTPClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		http: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

func (c *HTTPClient) Register(ctx context.Context, req protocol.RegisterRequest) (protocol.RegisterResponse, error) {
	var env protocol.RegisterResponseEnvelope
	if err := c.postJSON(ctx, "/remote/register", req, &env); err != nil {
		return protocol.RegisterResponse{}, err
	}
	return env.Payload, nil
}

func (c *HTTPClient) Heartbeat(ctx context.Context, req protocol.Heartbeat) error {
	return c.postJSON(ctx, "/remote/heartbeat", req, nil)
}

func (c *HTTPClient) Poll(ctx context.Context, req protocol.PollRequest) (protocol.RelayEnvelope, error) {
	var env protocol.RelayEnvelope
	if err := c.postJSON(ctx, "/remote/poll", req, &env); err != nil {
		return protocol.RelayEnvelope{}, err
	}
	return env, nil
}

func (c *HTTPClient) SendResult(ctx context.Context, req protocol.ResultRequest) error {
	return c.postJSON(ctx, "/remote/result", req, nil)
}

func (c *HTTPClient) Disconnect(ctx context.Context, req protocol.DisconnectRequest) error {
	return c.postJSON(ctx, "/remote/disconnect", req, nil)
}

func (c *HTTPClient) postJSON(ctx context.Context, path string, reqBody any, out any) error {
	buf, err := json.Marshal(reqBody)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(buf))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode >= 400 {
		return fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	if out == nil || len(body) == 0 {
		return nil
	}
	return json.Unmarshal(body, out)
}
