//go:build !windows

package process

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestProcessStartPollAndIdempotency(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	args := map[string]any{
		"process_id": "p1", "idempotency_key": "key1",
		"command": "printf hello", "cwd": root,
		"deadline_unix_ms": time.Now().Add(5 * time.Second).UnixMilli(),
	}
	started := manager.Execute(request("process.start", args))
	if !started.OK || started.Data["process_id"] != "p1" {
		t.Fatalf("unexpected start: %#v", started)
	}
	reused := manager.Execute(request("process.start", args))
	if !reused.OK || reused.Data["reused"] != true {
		t.Fatalf("expected idempotent reuse: %#v", reused)
	}
	result := waitDone(t, manager, "p1")
	if result.Data["stdout_all"] != "hello" || result.Data["exit_code"] != 0 {
		t.Fatalf("unexpected process result: %#v", result)
	}
}

func TestProcessInputWritesAndClosesStdin(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	started := manager.Execute(request("process.start", map[string]any{
		"process_id": "stdin", "idempotency_key": "stdin-key",
		"command": "read value; printf '%s' \"$value\"", "cwd": root,
		"deadline_unix_ms": time.Now().Add(5 * time.Second).UnixMilli(),
	}))
	if !started.OK {
		t.Fatal(started)
	}
	written := manager.Execute(request("process.input", map[string]any{
		"process_id": "stdin", "data": "hello\n", "close": true,
	}))
	if !written.OK || written.Data["bytes_written"] != len("hello\n") {
		t.Fatalf("unexpected input result: %#v", written)
	}
	result := waitDone(t, manager, "stdin")
	if result.Data["stdout_all"] != "hello" {
		t.Fatalf("unexpected process output: %#v", result)
	}
}

func TestProcessCancelAndDeadline(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	start := manager.Execute(request("process.start", map[string]any{
		"process_id": "cancel", "idempotency_key": "cancel-key",
		"command": "sleep 30", "cwd": root,
		"deadline_unix_ms": time.Now().Add(10 * time.Second).UnixMilli(),
	}))
	if !start.OK {
		t.Fatal(start)
	}
	cancelled := manager.Execute(request("process.cancel", map[string]any{"process_id": "cancel"}))
	if !cancelled.OK || cancelled.Data["cancelled"] != true {
		t.Fatal(cancelled)
	}
	released := manager.Execute(request("process.poll", map[string]any{"process_id": "cancel"}))
	if released.OK || released.ErrorCode != "not_found" {
		t.Fatalf("expected cancelled process state to be released: %#v", released)
	}

	start = manager.Execute(request("process.start", map[string]any{
		"process_id": "timeout", "idempotency_key": "timeout-key",
		"command": "sleep 30", "cwd": root,
		"deadline_unix_ms": time.Now().Add(50 * time.Millisecond).UnixMilli(),
	}))
	if !start.OK {
		t.Fatal(start)
	}
	result := waitDone(t, manager, "timeout")
	if result.Data["timed_out"] != true {
		t.Fatalf("expected timed out process: %#v", result)
	}
}

func TestProcessRejectsCWDOutsideWorkspace(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	result := manager.Execute(request("process.start", map[string]any{
		"process_id": "escape", "idempotency_key": "escape-key",
		"command": "pwd", "cwd": filepath.Dir(root),
	}))
	if result.OK || result.ErrorCode != "path_outside_workspace" {
		t.Fatalf("expected confinement failure: %#v", result)
	}
}

func waitDone(t *testing.T, manager *Manager, processID string) protocol.WorkspaceResult {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		result := manager.Execute(request("process.poll", map[string]any{
			"process_id": processID, "stdout_offset": 0, "stderr_offset": 0,
		}))
		if !result.OK {
			t.Fatal(result)
		}
		if result.Data["done"] == true {
			return result
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("process did not finish")
	return protocol.WorkspaceResult{}
}

func request(operation string, args map[string]any) protocol.WorkspaceRequest {
	return protocol.WorkspaceRequest{Operation: operation, Args: args}
}
