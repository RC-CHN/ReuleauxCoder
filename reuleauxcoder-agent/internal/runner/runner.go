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
	cfg    Config
	client *client.HTTPClient
}

func New(cfg Config) *Runner {
	return &Runner{
		cfg:    cfg,
		client: client.New(cfg.Host),
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
	scanner := bufio.NewScanner(os.Stdin)
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		fmt.Print("You > ")
		if !scanner.Scan() {
			if err := scanner.Err(); err != nil {
				return err
			}
			return nil
		}
		userInput := strings.TrimSpace(scanner.Text())
		if userInput == "" {
			continue
		}
		if userInput == "/quit" || userInput == "/exit" {
			return nil
		}

		chatCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
		resp, err := r.client.Chat(chatCtx, protocol.ChatRequest{
			PeerToken: peerToken,
			Prompt:    userInput,
		})
		cancel()
		if err != nil {
			return fmt.Errorf("chat failed: %w", err)
		}
		if strings.TrimSpace(resp.Error) != "" {
			fmt.Printf("Error: %s\n", resp.Error)
			continue
		}
		if resp.Response != "" {
			fmt.Printf("%s\n", resp.Response)
		}
	}
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
