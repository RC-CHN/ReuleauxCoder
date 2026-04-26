package protocol

import "encoding/json"

type RegisterRequest struct {
	BootstrapToken string         `json:"bootstrap_token"`
	HostInfoMin    map[string]any `json:"host_info_min,omitempty"`
	CWD            string         `json:"cwd,omitempty"`
	WorkspaceRoot  string         `json:"workspace_root,omitempty"`
	Capabilities   []string       `json:"capabilities,omitempty"`
}

type RegisterResponseEnvelope struct {
	Type    string           `json:"type"`
	Payload RegisterResponse `json:"payload"`
}

type RegisterRejectedEnvelope struct {
	Type    string           `json:"type"`
	Payload RegisterRejected `json:"payload"`
}

type RegisterResponse struct {
	PeerID               string `json:"peer_id"`
	PeerToken            string `json:"peer_token"`
	HeartbeatIntervalSec int    `json:"heartbeat_interval_sec"`
}

type RegisterRejected struct {
	Reason string `json:"reason"`
}

type Heartbeat struct {
	PeerToken string  `json:"peer_token"`
	TS        float64 `json:"ts"`
}

type RelayEnvelope struct {
	Type      string         `json:"type"`
	RequestID string         `json:"request_id,omitempty"`
	PeerID    string         `json:"peer_id,omitempty"`
	Payload   map[string]any `json:"payload,omitempty"`
}

type PollRequest struct {
	PeerToken string `json:"peer_token"`
}

type ResultRequest struct {
	PeerToken string         `json:"peer_token"`
	RequestID string         `json:"request_id"`
	Type      string         `json:"type"`
	Payload   map[string]any `json:"payload"`
}

type DisconnectRequest struct {
	PeerToken string `json:"peer_token"`
	Reason    string `json:"reason"`
}

type ChatRequest struct {
	PeerToken string `json:"peer_token"`
	Prompt    string `json:"prompt"`
}

type ChatResponse struct {
	Response string `json:"response"`
	Error    string `json:"error,omitempty"`
}

type ChatStartRequest struct {
	PeerToken   string `json:"peer_token"`
	Prompt      string `json:"prompt"`
	SessionHint string `json:"session_hint,omitempty"`
}

type ChatStartResponse struct {
	ChatID string `json:"chat_id"`
	Error  string `json:"error,omitempty"`
}

type ChatStreamRequest struct {
	PeerToken  string  `json:"peer_token"`
	ChatID     string  `json:"chat_id"`
	Cursor     int     `json:"cursor"`
	TimeoutSec float64 `json:"timeout_sec,omitempty"`
}

type ChatEvent struct {
	ChatID  string         `json:"chat_id"`
	Seq     int            `json:"seq"`
	Type    string         `json:"type"`
	Payload map[string]any `json:"payload,omitempty"`
}

type ChatStreamResponse struct {
	Events     []ChatEvent `json:"events,omitempty"`
	Done       bool        `json:"done"`
	NextCursor int         `json:"next_cursor"`
	Error      string      `json:"error,omitempty"`
}

type ApprovalReplyRequest struct {
	PeerToken  string `json:"peer_token"`
	ChatID     string `json:"chat_id"`
	ApprovalID string `json:"approval_id"`
	Decision   string `json:"decision"`
	Reason     string `json:"reason,omitempty"`
}

type ApprovalReplyResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

type ExecToolRequest struct {
	ToolName   string         `json:"tool_name"`
	Args       map[string]any `json:"args"`
	CWD        *string        `json:"cwd"`
	TimeoutSec int            `json:"timeout_sec"`
}

type ExecToolResult struct {
	OK           bool           `json:"ok"`
	Result       string         `json:"result,omitempty"`
	ErrorCode    string         `json:"error_code,omitempty"`
	ErrorMessage string         `json:"error_message,omitempty"`
	Meta         map[string]any `json:"meta,omitempty"`
}

type ToolStreamChunk struct {
	ChunkType string         `json:"chunk_type"`
	Data      string         `json:"data,omitempty"`
	Meta      map[string]any `json:"meta,omitempty"`
}

type CleanupResult struct {
	OK           bool     `json:"ok"`
	RemovedItems []string `json:"removed_items,omitempty"`
	ErrorMessage string   `json:"error_message,omitempty"`
}

type NoopEnvelope struct {
	Type    string         `json:"type"`
	Payload map[string]any `json:"payload"`
}

// DecodeExecToolRequest converts a map payload to ExecToolRequest via JSON roundtrip.
// The marshal-unmarshal pattern is idiomatic Go for map-to-struct conversion.
func DecodeExecToolRequest(payload map[string]any) (ExecToolRequest, error) {
	var req ExecToolRequest
	buf, err := json.Marshal(payload)
	if err != nil {
		return req, err
	}
	err = json.Unmarshal(buf, &req)
	return req, err
}
