//go:build !windows

package runner

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/client"
	processops "github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/process"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestLongPollDoesNotBlockControlOrPeerShutdown(t *testing.T) {
	for _, stopProcess := range []bool{true, false} {
		name := "shutdown"
		if stopProcess {
			name = "terminate"
		}
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			manager := processops.NewManager(root, root)
			defer manager.Close()
			started := manager.Execute(protocol.WorkspaceRequest{Operation: "process.start", Args: map[string]any{
				"process_id": "p", "idempotency_key": "p", "command": "sleep 30", "cwd": root,
			}})
			if !started.OK {
				t.Fatalf("start: %#v", started)
			}
			requests := make(chan protocol.RelayEnvelope, 2)
			results := make(chan protocol.ResultRequest, 4)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				switch req.URL.Path {
				case "/remote/poll":
					select {
					case envelope := <-requests:
						_ = json.NewEncoder(w).Encode(envelope)
					case <-ctx.Done():
					case <-req.Context().Done():
					}
				case "/remote/result":
					var result protocol.ResultRequest
					if err := json.NewDecoder(req.Body).Decode(&result); err != nil {
						t.Error(err)
						return
					}
					results <- result
					_, _ = w.Write([]byte("{}"))
				default:
					http.NotFound(w, req)
				}
			}))
			defer server.Close()
			defer cancel()
			r := &Runner{client: client.New(server.URL)}
			done := make(chan error, 1)
			go func() { done <- r.runPollLoop(ctx, "token", root, root, time.Millisecond, manager) }()
			send := func(id, operation string, args map[string]any) {
				requests <- protocol.RelayEnvelope{Type: "workspace_request", RequestID: id, Payload: mapFromStruct(protocol.WorkspaceRequest{Operation: operation, Args: args})}
			}
			send("wait", "process.poll", map[string]any{"process_id": "p", "wait_ms": 30000})
			select {
			case result := <-results:
				t.Fatalf("long poll returned early: %#v", result)
			case <-time.After(100 * time.Millisecond):
			}
			if stopProcess {
				send("stop", "process.terminate", map[string]any{"process_id": "p"})
				seen := map[string]bool{}
				timer := time.NewTimer(3 * time.Second)
				defer timer.Stop()
				for len(seen) < 2 {
					select {
					case result := <-results:
						if result.Payload["ok"] != true {
							t.Fatalf("failed response: %#v", result)
						}
						seen[result.RequestID] = true
					case <-timer.C:
						t.Fatal("long poll blocked process control")
					}
				}
			}
			cancel()
			select {
			case err := <-done:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(3 * time.Second):
				t.Fatal("peer shutdown left a poll worker running")
			}
		})
	}
}
