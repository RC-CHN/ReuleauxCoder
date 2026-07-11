package runner

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math/rand"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/client"
	processops "github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/process"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/tools"
	workspaceops "github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/workspace"
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
	cfg      Config
	client   *client.HTTPClient
	scanner  *bufio.Scanner
	lines    chan string
	inputErr chan error
}

func New(cfg Config) *Runner {
	return &Runner{
		cfg:      cfg,
		client:   client.New(cfg.Host),
		scanner:  bufio.NewScanner(os.Stdin),
		lines:    make(chan string),
		inputErr: make(chan error, 1),
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
	processManager := processops.NewManager(workspaceRoot, cwd)
	defer processManager.Close()

	registerResp, err := r.client.Register(ctx, protocol.RegisterRequest{
		BootstrapToken: r.cfg.BootstrapToken,
		CWD:            cwd,
		WorkspaceRoot:  workspaceRoot,
		Capabilities: []string{
			"shell", "process.start", "process.input", "process.poll", "process.cancel",
			"workspace.fs.stat", "workspace.fs.list", "workspace.fs.read_text",
			"workspace.fs.write_text_atomic", "workspace.fs.replace_exact_atomic",
		},
		ProtocolVersion: 2,
		Terminal:        detectTerminalCapabilities(r.cfg.Interactive),
		HostInfoMin: map[string]any{
			"os":       runtimeOS(),
			"arch":     runtimeArch(),
			"hostname": runtimeHostname(),
		},
	})
	if err != nil {
		return fmt.Errorf("register failed: %w", err)
	}
	if registerResp.ProtocolVersion == 0 {
		registerResp.ProtocolVersion = 1
	}
	if registerResp.ProtocolVersion < 1 || registerResp.ProtocolVersion > 2 {
		return fmt.Errorf(
			"host negotiated unsupported protocol version %d",
			registerResp.ProtocolVersion,
		)
	}
	log.Printf("registered peer_id=%s", registerResp.PeerID)
	fmt.Printf("Connected to %s as %s (%s)\n", r.cfg.Host, registerResp.PeerID, workspaceRoot)

	heartbeatInterval := time.Duration(registerResp.HeartbeatIntervalSec) * time.Second
	if heartbeatInterval <= 0 {
		heartbeatInterval = 10 * time.Second
	}
	pollInterval := r.cfg.PollInterval
	if pollInterval <= 0 {
		pollInterval = 500 * time.Millisecond
	}

	var childCtx context.Context
	var cancel context.CancelFunc
	interrupts := make(chan os.Signal, 1)
	if r.cfg.Interactive {
		childCtx, cancel = signal.NotifyContext(ctx, syscall.SIGTERM)
		signal.Notify(interrupts, os.Interrupt)
		defer signal.Stop(interrupts)
		r.startInputPump()
	} else {
		childCtx, cancel = signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	}
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
			errCh <- r.runPollLoop(childCtx, registerResp.PeerToken, workspaceRoot, cwd, pollInterval, processManager)
		}()

		if err := r.runInteractiveLoop(childCtx, registerResp.PeerToken, interrupts); err != nil {
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

	return r.runPollLoop(childCtx, registerResp.PeerToken, workspaceRoot, cwd, pollInterval, processManager)
}

func detectTerminalCapabilities(interactive bool) protocol.TerminalCapabilities {
	width := 80
	if parsed, err := strconv.Atoi(os.Getenv("COLUMNS")); err == nil && parsed >= 20 {
		width = min(parsed, 500)
	}
	colorLevel := "none"
	term := strings.ToLower(os.Getenv("TERM"))
	colorTerm := strings.ToLower(os.Getenv("COLORTERM"))
	if os.Getenv("NO_COLOR") == "" && term != "" && term != "dumb" {
		colorLevel = "standard"
		if strings.Contains(term, "256color") {
			colorLevel = "256"
		}
		if strings.Contains(colorTerm, "truecolor") || strings.Contains(colorTerm, "24bit") {
			colorLevel = "truecolor"
		}
	}
	locale := strings.ToLower(os.Getenv("LC_ALL") + os.Getenv("LC_CTYPE") + os.Getenv("LANG"))
	unicode := strings.Contains(locale, "utf-8") || strings.Contains(locale, "utf8")
	return protocol.TerminalCapabilities{
		Width: width, ColorLevel: colorLevel, Unicode: unicode, Interactive: interactive,
	}
}

func (r *Runner) runPollLoop(
	ctx context.Context,
	peerToken, workspaceRoot, cwd string,
	pollInterval time.Duration,
	processManager *processops.Manager,
) error {
	retryDelay := 500 * time.Millisecond
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		pollCtx, cancelPoll := context.WithTimeout(ctx, 35*time.Second)
		env, err := r.client.Poll(pollCtx, protocol.PollRequest{
			PeerToken:  peerToken,
			TimeoutSec: 25,
		})
		cancelPoll()
		if err != nil {
			if isAuthenticationError(err) {
				if refreshErr := r.refreshLease(ctx, peerToken); refreshErr == nil {
					log.Print("peer lease refreshed after poll authentication failure")
					continue
				} else {
					return fmt.Errorf(
						"peer lease expired and refresh failed; obtain a new bootstrap token: %w",
						refreshErr,
					)
				}
			}
			log.Printf("poll failed; reconnecting: %v", err)
			if !waitForRetry(ctx, jitteredBackoff(retryDelay)) {
				return nil
			}
			retryDelay = min(retryDelay*2, 15*time.Second)
			continue
		}
		retryDelay = 500 * time.Millisecond

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
		case "workspace_request":
			workspaceReq, err := protocol.DecodeWorkspaceRequest(env.Payload)
			if err != nil {
				result := protocol.WorkspaceResult{OK: false, ErrorCode: "invalid_path", ErrorMessage: err.Error()}
				if sendErr := r.sendWorkspaceResult(ctx, peerToken, env.RequestID, result); sendErr != nil {
					return sendErr
				}
				continue
			}
			var result protocol.WorkspaceResult
			if strings.HasPrefix(workspaceReq.Operation, "process.") {
				result = processManager.Execute(workspaceReq)
			} else {
				result = workspaceops.Execute(workspaceReq, workspaceRoot, cwd)
			}
			if sendErr := r.sendWorkspaceResult(ctx, peerToken, env.RequestID, result); sendErr != nil {
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

func (r *Runner) startInputPump() {
	go func() {
		defer close(r.lines)
		for r.scanner.Scan() {
			r.lines <- r.scanner.Text()
		}
		r.inputErr <- r.scanner.Err()
	}()
}

func (r *Runner) runInteractiveLoop(
	ctx context.Context, peerToken string, interrupts <-chan os.Signal,
) error {
	var lastIdleInterrupt time.Time
	for {
		fmt.Print("You > ")
		var rawInput string
		select {
		case <-ctx.Done():
			return nil
		case <-interrupts:
			now := time.Now()
			if !lastIdleInterrupt.IsZero() && now.Sub(lastIdleInterrupt) <= 2*time.Second {
				fmt.Println("\nExiting.")
				return nil
			}
			lastIdleInterrupt = now
			fmt.Println("\nPress Ctrl+C again within 2s to exit.")
			continue
		case line, ok := <-r.lines:
			if !ok {
				return <-r.inputErr
			}
			rawInput = line
		}
		lastIdleInterrupt = time.Time{}
		userInput := strings.TrimSpace(rawInput)
		if userInput == "" {
			continue
		}
		if userInput == "/quit" || userInput == "/exit" {
			return nil
		}
		if err := r.runRemoteChat(ctx, peerToken, userInput, interrupts); err != nil {
			if err == errChatCancelled {
				fmt.Println("\nCancelled.")
				continue
			}
			fmt.Fprintf(os.Stderr, "\nRemote chat error: %v\n", err)
			continue
		}
	}
}

var errChatCancelled = errors.New("remote chat cancelled")

func (r *Runner) runRemoteChat(
	ctx context.Context,
	peerToken, prompt string,
	interrupts <-chan os.Signal,
) error {
	chatCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	startResp, err := r.client.ChatStart(chatCtx, protocol.ChatStartRequest{
		PeerToken: peerToken,
		Prompt:    prompt,
	})
	cancel()
	if isAuthenticationError(err) {
		if refreshErr := r.refreshLease(ctx, peerToken); refreshErr != nil {
			return fmt.Errorf(
				"chat lease expired and refresh failed; obtain a new bootstrap token: %w",
				refreshErr,
			)
		}
		chatCtx, cancel = context.WithTimeout(ctx, 10*time.Minute)
		startResp, err = r.client.ChatStart(chatCtx, protocol.ChatStartRequest{
			PeerToken: peerToken,
			Prompt:    prompt,
		})
		cancel()
	}
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
	retryDelay := 500 * time.Millisecond
	for {
		streamCtx, cancel := context.WithTimeout(ctx, 35*time.Second)
		type streamResult struct {
			response protocol.ChatStreamResponse
			err      error
		}
		resultCh := make(chan streamResult, 1)
		go func() {
			response, streamErr := r.client.ChatStream(streamCtx, protocol.ChatStreamRequest{
				PeerToken: peerToken, ChatID: startResp.ChatID,
				Cursor: cursor, TimeoutSec: 30,
			})
			resultCh <- streamResult{response: response, err: streamErr}
		}()
		var streamResp protocol.ChatStreamResponse
		select {
		case <-ctx.Done():
			cancel()
			return nil
		case <-interrupts:
			cancel()
			r.cancelRemoteChat(peerToken, startResp.ChatID)
			return errChatCancelled
		case result := <-resultCh:
			cancel()
			streamResp, err = result.response, result.err
		}
		if err != nil {
			if isAuthenticationError(err) {
				if refreshErr := r.refreshLease(ctx, peerToken); refreshErr == nil {
					continue
				} else {
					return fmt.Errorf(
						"chat lease expired and refresh failed; obtain a new bootstrap token: %w",
						refreshErr,
					)
				}
			}
			if !waitForRetry(ctx, jitteredBackoff(retryDelay)) {
				return nil
			}
			retryDelay = min(retryDelay*2, 15*time.Second)
			continue
		}
		retryDelay = 500 * time.Millisecond
		if strings.TrimSpace(streamResp.Error) != "" {
			return fmt.Errorf("chat stream failed: %s", streamResp.Error)
		}
		for _, event := range streamResp.Events {
			if err := r.handleChatEvent(ctx, peerToken, startResp.ChatID, event, interrupts); err != nil {
				return err
			}
		}
		cursor = streamResp.NextCursor
		if streamResp.Done {
			return nil
		}
	}
}

func (r *Runner) cancelRemoteChat(peerToken, chatID string) {
	cancelCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, _ = r.client.ChatCancel(cancelCtx, protocol.ChatCancelRequest{
		PeerToken: peerToken, ChatID: chatID, Reason: "user_interrupt",
	})
}

func jitteredBackoff(base time.Duration) time.Duration {
	if base <= 0 {
		return 0
	}
	return base + time.Duration(rand.Int63n(int64(base/2)+1))
}

func waitForRetry(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func isAuthenticationError(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "http 401") || strings.Contains(message, "http 403")
}

func (r *Runner) refreshLease(ctx context.Context, peerToken string) error {
	refreshCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	response, err := r.client.RefreshToken(
		refreshCtx,
		protocol.TokenRefreshRequest{PeerToken: peerToken},
	)
	if err != nil {
		return err
	}
	if !response.OK || response.PeerToken != peerToken {
		return fmt.Errorf("lease refresh rejected: %s", response.Error)
	}
	return nil
}

func (r *Runner) handleChatEvent(
	ctx context.Context,
	peerToken, chatID string,
	event protocol.ChatEvent,
	interrupts <-chan os.Signal,
) error {
	switch event.Type {
	case "chat_start":
		return nil
	case "output":
		r.renderOutputEvent(event.Payload)
	case "tool_call_stream":
		r.renderToolStream(event.Payload)
	case "interaction_request":
		return r.handleInteractionRequest(ctx, peerToken, chatID, event.Payload, interrupts)
	case "interaction_resolved":
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

func (r *Runner) handleInteractionRequest(
	ctx context.Context,
	peerToken, chatID string,
	payload map[string]any,
	interrupts <-chan os.Signal,
) error {
	if frame, _ := payload["rendered_frame"].(string); frame != "" {
		fmt.Print(frame)
	}
	requestID, _ := payload["request_id"].(string)
	if requestID == "" {
		return fmt.Errorf("interaction request missing request_id")
	}

	fmt.Print("Respond? [y/N]: ")
	value := false
	cancelled := false
	select {
	case <-ctx.Done():
		cancelled = true
	case <-interrupts:
		r.cancelRemoteChat(peerToken, chatID)
		return errChatCancelled
	case line, ok := <-r.lines:
		if !ok {
			cancelled = true
			break
		}
		answer := strings.ToLower(strings.TrimSpace(line))
		if answer == "y" || answer == "yes" || answer == "a" || answer == "allow" {
			value = true
		}
	}

	replyCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	replyResp, err := r.client.InteractionReply(replyCtx, protocol.InteractionReplyRequest{
		PeerToken: peerToken,
		ChatID:    chatID,
		RequestID: requestID,
		Value:     value,
		Cancelled: cancelled,
	})
	if isAuthenticationError(err) {
		if refreshErr := r.refreshLease(ctx, peerToken); refreshErr == nil {
			replyResp, err = r.client.InteractionReply(replyCtx, protocol.InteractionReplyRequest{
				PeerToken: peerToken, ChatID: chatID, RequestID: requestID,
				Value: value, Cancelled: cancelled,
			})
		}
	}
	if err != nil {
		return fmt.Errorf("interaction reply failed: %w", err)
	}
	if !replyResp.OK {
		return fmt.Errorf("interaction reply failed: %s", replyResp.Error)
	}
	return nil
}

func (r *Runner) renderOutputEvent(payload map[string]any) {
	content, _ := payload["content"].(string)
	if content == "" {
		return
	}

	fmt.Print(content)

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
				if isAuthenticationError(err) {
					if refreshErr := r.refreshLease(ctx, peerToken); refreshErr != nil {
						log.Printf("heartbeat lease refresh failed: %v", refreshErr)
					}
				} else {
					log.Printf("heartbeat failed: %v", err)
				}
			}
		}
	}
}

func (r *Runner) sendToolResult(ctx context.Context, peerToken, requestID string, result protocol.ExecToolResult) error {
	return r.sendResultWithLeaseRefresh(ctx, peerToken, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "tool_result",
		Payload:   mapFromStruct(result),
	})
}

func (r *Runner) sendToolStream(ctx context.Context, peerToken, requestID string, chunk protocol.ToolStreamChunk) error {
	return r.sendResultWithLeaseRefresh(ctx, peerToken, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "tool_stream",
		Payload:   mapFromStruct(chunk),
	})
}

func (r *Runner) sendWorkspaceResult(ctx context.Context, peerToken, requestID string, result protocol.WorkspaceResult) error {
	return r.sendResultWithLeaseRefresh(ctx, peerToken, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "workspace_result",
		Payload:   mapFromStruct(result),
	})
}

func (r *Runner) sendCleanupResult(ctx context.Context, peerToken, requestID string, result protocol.CleanupResult) error {
	return r.sendResultWithLeaseRefresh(ctx, peerToken, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "cleanup_result",
		Payload:   mapFromStruct(result),
	})
}

func (r *Runner) sendResultWithLeaseRefresh(
	ctx context.Context,
	peerToken string,
	request protocol.ResultRequest,
) error {
	send := func() error {
		sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		defer cancel()
		return r.client.SendResult(sendCtx, request)
	}
	err := send()
	if !isAuthenticationError(err) {
		return err
	}
	if refreshErr := r.refreshLease(ctx, peerToken); refreshErr != nil {
		return fmt.Errorf("result lease refresh failed: %w", refreshErr)
	}
	return send()
}

// mapFromStruct converts a struct to map[string]any via JSON roundtrip.
// This is the idiomatic Go approach for struct-to-map conversion;
// the intermediate JSON marshal is cheap for small control-plane structs.
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

func runtimeHostname() string {
	hostname, err := os.Hostname()
	if err != nil {
		return ""
	}
	return hostname
}
