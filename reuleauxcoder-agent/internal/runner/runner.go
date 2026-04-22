package runner

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/client"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/tools"
	"github.com/charmbracelet/glamour"
)

type Config struct {
	Host           string
	BootstrapToken string
	CWD            string
	WorkspaceRoot  string
	PollInterval   time.Duration
	Interactive    bool
}

type Runner struct {
	cfg       Config
	client    *client.HTTPClient
	scanner   *bufio.Scanner
	mdRender  *glamour.TermRenderer
}

func New(cfg Config) *Runner {
	renderer, err := glamour.NewTermRenderer(
		glamour.WithAutoStyle(),
		glamour.WithWordWrap(100),
	)
	if err != nil {
		log.Printf("markdown renderer init failed: %v", err)
	}
	return &Runner{
		cfg:      cfg,
		client:   client.New(cfg.Host),
		scanner:  bufio.NewScanner(os.Stdin),
		mdRender: renderer,
	}
}

func (r *Runner) Run(ctx context.Context) error {
	cwd := r.cfg.CWD
	if cwd == "" {
		resolved, err := os.Getwd()
		if err != nil {
			return err
		}
		cwd = resolved
	}
	workspaceRoot := r.cfg.WorkspaceRoot
	if workspaceRoot == "" {
		workspaceRoot = cwd
	}

	registerResp, err := r.client.Register(ctx, protocol.RegisterRequest{
		BootstrapToken: r.cfg.BootstrapToken,
		CWD:            cwd,
		WorkspaceRoot:  workspaceRoot,
		Capabilities:   []string{"shell", "read_file", "write_file", "edit_file", "glob", "grep"},
		HostInfoMin: map[string]any{
			"os":   runtimeOS(),
			"arch": runtimeArch(),
		},
	})
	if err != nil {
		return fmt.Errorf("register failed: %w", err)
	}
	log.Printf("registered peer_id=%s", registerResp.PeerID)
	fmt.Printf("\n=== REMOTE PEER CONNECTED ===\nPeer: %s\nWorkspace: %s\nHost: %s\n============================\n\n", registerResp.PeerID, workspaceRoot, r.cfg.Host)

	heartbeatInterval := time.Duration(registerResp.HeartbeatIntervalSec) * time.Second
	if heartbeatInterval <= 0 {
		heartbeatInterval = 10 * time.Second
	}
	pollInterval := r.cfg.PollInterval
	if pollInterval <= 0 {
		pollInterval = 500 * time.Millisecond
	}

	childCtx, cancel := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer cancel()
	defer func() {
		disconnectCtx, cancelDisconnect := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancelDisconnect()
		_ = r.client.Disconnect(disconnectCtx, protocol.DisconnectRequest{
			PeerToken: registerResp.PeerToken,
			Reason:    "peer_shutdown",
		})
	}()

	go r.heartbeatLoop(childCtx, registerResp.PeerToken, heartbeatInterval)

	if r.cfg.Interactive {
		errCh := make(chan error, 1)
		go func() {
			errCh <- r.runPollLoop(childCtx, registerResp.PeerToken, cwd, pollInterval)
		}()

		if err := r.runInteractiveLoop(childCtx, registerResp.PeerToken); err != nil {
			return err
		}
		cancel()
		select {
		case err := <-errCh:
			if err != nil && childCtx.Err() == nil {
				return err
			}
		default:
		}
		return nil
	}

	return r.runPollLoop(childCtx, registerResp.PeerToken, cwd, pollInterval)
}

func (r *Runner) runPollLoop(ctx context.Context, peerToken, cwd string, pollInterval time.Duration) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		pollCtx, cancelPoll := context.WithTimeout(ctx, 30*time.Second)
		env, err := r.client.Poll(pollCtx, protocol.PollRequest{PeerToken: peerToken})
		cancelPoll()
		if err != nil {
			return fmt.Errorf("poll failed: %w", err)
		}

		switch env.Type {
		case "noop", "":
			time.Sleep(pollInterval)
			continue
		case "exec_tool":
			execReq, err := protocol.DecodeExecToolRequest(env.Payload)
			if err != nil {
				if sendErr := r.sendToolResult(ctx, peerToken, env.RequestID, protocol.ExecToolResult{
					OK:           false,
					ErrorCode:    "REMOTE_TOOL_ERROR",
					ErrorMessage: err.Error(),
				}); sendErr != nil {
					return sendErr
				}
				continue
			}
			result := tools.Execute(execReq, cwd, func(chunk protocol.ToolStreamChunk) {
				if sendErr := r.sendToolStream(ctx, peerToken, env.RequestID, chunk); sendErr != nil {
					log.Printf("stream send failed: %v", sendErr)
				}
			})
			if sendErr := r.sendToolResult(ctx, peerToken, env.RequestID, result); sendErr != nil {
				return sendErr
			}
		case "cleanup":
			cleanup := protocol.CleanupResult{OK: true, RemovedItems: []string{}}
			if err := r.sendCleanupResult(ctx, peerToken, env.RequestID, cleanup); err != nil {
				return err
			}
		default:
			log.Printf("ignoring unsupported envelope type=%s", env.Type)
			time.Sleep(pollInterval)
		}
	}
}

func (r *Runner) runInteractiveLoop(ctx context.Context, peerToken string) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		fmt.Print("You > ")
		if !r.scanner.Scan() {
			if err := r.scanner.Err(); err != nil {
				return err
			}
			return nil
		}
		userInput := strings.TrimSpace(r.scanner.Text())
		if userInput == "" {
			continue
		}
		if userInput == "/quit" || userInput == "/exit" {
			return nil
		}
		if err := r.runRemoteChat(ctx, peerToken, userInput); err != nil {
			return err
		}
	}
}

func (r *Runner) runRemoteChat(ctx context.Context, peerToken, prompt string) error {
	chatCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	startResp, err := r.client.ChatStart(chatCtx, protocol.ChatStartRequest{
		PeerToken: peerToken,
		Prompt:    prompt,
	})
	cancel()
	if err != nil {
		return fmt.Errorf("chat start failed: %w", err)
	}
	if strings.TrimSpace(startResp.Error) != "" {
		return fmt.Errorf("chat start failed: %s", startResp.Error)
	}
	if strings.TrimSpace(startResp.ChatID) == "" {
		return fmt.Errorf("chat start failed: empty chat id")
	}

	cursor := 0
	for {
		streamCtx, cancel := context.WithTimeout(ctx, 35*time.Second)
		streamResp, err := r.client.ChatStream(streamCtx, protocol.ChatStreamRequest{
			PeerToken:  peerToken,
			ChatID:     startResp.ChatID,
			Cursor:     cursor,
			TimeoutSec: 30,
		})
		cancel()
		if err != nil {
			return fmt.Errorf("chat stream failed: %w", err)
		}
		if strings.TrimSpace(streamResp.Error) != "" {
			return fmt.Errorf("chat stream failed: %s", streamResp.Error)
		}
		for _, event := range streamResp.Events {
			if err := r.handleChatEvent(ctx, peerToken, startResp.ChatID, event); err != nil {
				return err
			}
		}
		cursor = streamResp.NextCursor
		if streamResp.Done {
			return nil
		}
	}
}

func (r *Runner) handleChatEvent(ctx context.Context, peerToken, chatID string, event protocol.ChatEvent) error {
	switch event.Type {
	case "chat_start":
		return nil
	case "output":
		r.renderOutputEvent(event.Payload)
	case "tool_call_stream":
		r.renderToolStream(event.Payload)
	case "approval_request":
		return r.handleApprovalRequest(ctx, peerToken, chatID, event.Payload)
	case "approval_resolved":
		return nil
	case "tool_call_start":
		if name, _ := event.Payload["tool_name"].(string); name != "" {
			fmt.Printf("\n[tool] %s\n", name)
		}
	case "tool_call_end":
		return nil
	case "chat_end":
		if response, _ := event.Payload["response"].(string); strings.TrimSpace(response) != "" {
			fmt.Println()
		}
	case "error":
		msg, _ := event.Payload["message"].(string)
		if msg == "" {
			msg = "unknown error"
		}
		fmt.Fprintf(os.Stderr, "\nError: %s\n", msg)
	}
	return nil
}

func (r *Runner) handleApprovalRequest(ctx context.Context, peerToken, chatID string, payload map[string]any) error {
	r.renderOutputEvent(payload)

	approvalID, _ := payload["approval_id"].(string)
	if approvalID == "" {
		return fmt.Errorf("approval request missing approval_id")
	}

	fmt.Print("Approve? [y/N]: ")
	decision := "deny_once"
	if r.scanner.Scan() {
		answer := strings.ToLower(strings.TrimSpace(r.scanner.Text()))
		if answer == "y" || answer == "yes" || answer == "a" || answer == "allow" {
			decision = "allow_once"
		}
	} else if err := r.scanner.Err(); err != nil {
		return err
	}

	replyCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	replyResp, err := r.client.ApprovalReply(replyCtx, protocol.ApprovalReplyRequest{
		PeerToken:  peerToken,
		ChatID:     chatID,
		ApprovalID: approvalID,
		Decision:   decision,
	})
	if err != nil {
		return fmt.Errorf("approval reply failed: %w", err)
	}
	if !replyResp.OK {
		return fmt.Errorf("approval reply failed: %s", replyResp.Error)
	}
	return nil
}

func (r *Runner) renderOutputEvent(payload map[string]any) {
	format, _ := payload["format"].(string)
	content, _ := payload["content"].(string)
	if content == "" {
		return
	}

	switch format {
	case "markdown":
		if r.mdRender != nil {
			rendered, err := r.mdRender.Render(content)
			if err == nil {
				fmt.Print(rendered)
				return
			}
		}
		fmt.Print(content)
	case "plain", "terminal", "":
		fmt.Print(content)
	default:
		fmt.Print(content)
	}

	if newline, ok := payload["newline"].(bool); ok && newline {
		fmt.Print("\n")
	}
}

func (r *Runner) renderToolStream(payload map[string]any) {
	content, _ := payload["content"].(string)
	if content == "" {
		return
	}
	stream, _ := payload["stream"].(string)
	if stream == "stderr" {
		fmt.Fprint(os.Stderr, content)
		return
	}
	fmt.Print(content)
}

func (r *Runner) heartbeatLoop(ctx context.Context, peerToken string, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			hbCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
			err := r.client.Heartbeat(hbCtx, protocol.Heartbeat{
				PeerToken: peerToken,
				TS:        float64(time.Now().UnixNano()) / 1e9,
			})
			cancel()
			if err != nil {
				log.Printf("heartbeat failed: %v", err)
			}
		}
	}
}

func (r *Runner) sendToolResult(ctx context.Context, peerToken, requestID string, result protocol.ExecToolResult) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	return r.client.SendResult(sendCtx, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "tool_result",
		Payload:   mapFromStruct(result),
	})
}

func (r *Runner) sendToolStream(ctx context.Context, peerToken, requestID string, chunk protocol.ToolStreamChunk) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	return r.client.SendResult(sendCtx, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "tool_stream",
		Payload:   mapFromStruct(chunk),
	})
}

func (r *Runner) sendCleanupResult(ctx context.Context, peerToken, requestID string, result protocol.CleanupResult) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	return r.client.SendResult(sendCtx, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "cleanup_result",
		Payload:   mapFromStruct(result),
	})
}

func mapFromStruct(v any) map[string]any {
	buf, err := json.Marshal(v)
	if err != nil {
		return map[string]any{}
	}
	out := map[string]any{}
	if err := json.Unmarshal(buf, &out); err != nil {
		return map[string]any{}
	}
	return out
}

func runtimeOS() string {
	return runtime.GOOS
}

func runtimeArch() string {
	return runtime.GOARCH
}
