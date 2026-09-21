package process

import (
	"context"
	"testing"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestOutputWakesAllConcurrentPollConsumers(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	processState := &state{id: "p", done: make(chan struct{}), changed: make(chan struct{}), startedAt: time.Now()}
	processState.stdout.onChange = processState.signalChange
	manager.states["p"] = processState
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	results := make(chan protocol.WorkspaceResult, 8)
	for range 8 {
		go func() {
			results <- manager.ExecuteContext(ctx, protocol.WorkspaceRequest{Operation: "process.poll", Args: map[string]any{"process_id": "p", "wait_ms": 30000}})
		}()
	}
	// Keep the process running after writing: each consumer must wake on output,
	// independently of another consumer or a process-exit notification.
	time.Sleep(100 * time.Millisecond)
	_, _ = processState.stdout.Write([]byte("ready"))
	timer := time.NewTimer(2 * time.Second)
	defer timer.Stop()
	for range 8 {
		select {
		case result := <-results:
			if !result.OK || result.Data["stdout"] != "ready" || result.Data["state"] != "running" {
				t.Fatalf("unexpected poll: %#v", result)
			}
		case <-timer.C:
			t.Fatal("output notification failed to wake every consumer")
		}
	}
}
