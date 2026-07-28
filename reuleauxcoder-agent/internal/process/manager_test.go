//go:build !windows

package process

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
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

func TestProcessCapturesOutputFromImmediatelyExitingCommands(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	for index := 0; index < 8; index++ {
		processID := fmt.Sprintf("quick-%d", index)
		started := manager.Execute(request("process.start", map[string]any{
			"process_id": processID, "idempotency_key": processID,
			"command": "printf quick-output", "cwd": root,
			"deadline_unix_ms": time.Now().Add(5 * time.Second).UnixMilli(),
		}))
		if !started.OK {
			t.Fatalf("start %d failed: %#v", index, started)
		}
		result := waitDone(t, manager, processID)
		if result.Data["stdout_all"] != "quick-output" {
			t.Fatalf("output %d was lost: %#v", index, result)
		}
	}
}

func TestRootExitDoesNotWaitIndefinitelyForDescendantPipe(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	startedAt := time.Now()
	started := manager.Execute(request("process.start", map[string]any{
		"process_id": "descendant-pipe", "idempotency_key": "descendant-pipe-key",
		"command": "sleep 30 & printf done", "cwd": root,
		"deadline_unix_ms": time.Now().Add(10 * time.Second).UnixMilli(),
	}))
	if !started.OK {
		t.Fatal(started)
	}
	result := waitDone(t, manager, "descendant-pipe")
	if elapsed := time.Since(startedAt); elapsed > 2*time.Second {
		t.Fatalf("root exit waited for descendant pipe: %s", elapsed)
	}
	if result.Data["exit_code"] != 0 || result.Data["stdout_all"] != "done" {
		t.Fatalf("root process result was not preserved: %#v", result)
	}
}

func TestProcessPassesShellOperatorsAndQuotingUnchanged(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	started := manager.Execute(request("process.start", map[string]any{
		"process_id": "syntax", "idempotency_key": "syntax-key",
		"command": "printf '%s' 'left && right'; printf '\\nsecond line\\n'",
		"cwd":     root, "tty": false,
	}))
	if !started.OK {
		t.Fatal(started)
	}
	result := waitDone(t, manager, "syntax")
	if result.Data["stdout_all"] != "left && right\nsecond line\n" {
		t.Fatalf("shell command semantics changed: %#v", result)
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

func TestProcessInputRejectsOversizedWrite(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	started := manager.Execute(request("process.start", map[string]any{
		"process_id": "stdin-limit", "idempotency_key": "stdin-limit-key",
		"command": "cat", "cwd": root,
		"deadline_unix_ms": time.Now().Add(5 * time.Second).UnixMilli(),
	}))
	if !started.OK {
		t.Fatal(started)
	}
	rejected := manager.Execute(request("process.input", map[string]any{
		"process_id": "stdin-limit", "data": strings.Repeat("x", maxInputBytes+1),
	}))
	if rejected.OK || rejected.ErrorCode != "resource_exhausted" {
		t.Fatalf("oversized input was not rejected: %#v", rejected)
	}
	manager.Execute(request("process.terminate", map[string]any{
		"process_id": "stdin-limit", "reason": "test_cleanup",
	}))
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

func TestBoundedBufferUsesMonotonicOffsetsAndUTF8Boundaries(t *testing.T) {
	var buffer boundedBuffer
	if _, err := buffer.Write([]byte{0xe4, 0xbd}); err != nil {
		t.Fatal(err)
	}
	partial := buffer.snapshot(0, maxPollBytesPerStream, false)
	if len(partial.data) != 0 || partial.nextOffset != 0 {
		t.Fatalf("incomplete rune should be withheld: %#v", partial)
	}
	if _, err := buffer.Write([]byte{0xa0}); err != nil {
		t.Fatal(err)
	}
	complete := buffer.snapshot(0, maxPollBytesPerStream, false)
	if string(complete.data) != "你" || complete.nextOffset != 3 {
		t.Fatalf("unexpected UTF-8 snapshot: %#v", complete)
	}
	if _, err := buffer.Write([]byte{0xff}); err != nil {
		t.Fatal(err)
	}
	invalid := buffer.snapshot(3, maxPollBytesPerStream, true)
	if !invalid.decodeReplaced || invalid.nextOffset != 4 {
		t.Fatalf("invalid output replacement must be reported: %#v", invalid)
	}

	large := make([]byte, maxRetainedBytesPerStream+1024)
	for index := range large {
		large[index] = 'x'
	}
	if _, err := buffer.Write(large); err != nil {
		t.Fatal(err)
	}
	bounded := buffer.snapshot(0, maxPollBytesPerStream, true)
	if len(bounded.data) > maxPollBytesPerStream || !bounded.truncated {
		t.Fatalf("large output was not bounded: %#v", bounded)
	}
	if bounded.totalBytes != int64(len(large)+4) {
		t.Fatalf("unexpected total bytes: %d", bounded.totalBytes)
	}
}

func TestTerminalProcessIsRetainedUntilExplicitRelease(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	started := manager.Execute(request("process.start", map[string]any{
		"process_id": "retained", "idempotency_key": "retained-key",
		"command": "printf retained", "cwd": root, "tty": false,
	}))
	if !started.OK {
		t.Fatal(started)
	}
	first := waitDone(t, manager, "retained")
	second := manager.Execute(request("process.poll", map[string]any{
		"process_id": "retained", "stdout_offset": 0,
	}))
	if !second.OK || first.Data["stdout_all"] != second.Data["stdout_all"] {
		t.Fatalf("terminal process was not retained: first=%#v second=%#v", first, second)
	}
	released := manager.Execute(request("process.release", map[string]any{
		"process_id": "retained",
	}))
	if !released.OK {
		t.Fatal(released)
	}
	missing := manager.Execute(request("process.poll", map[string]any{
		"process_id": "retained",
	}))
	if missing.OK || missing.ErrorCode != "not_found" {
		t.Fatalf("released process remained queryable: %#v", missing)
	}
}

func TestExplicitPipeModeRejectsInput(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	started := manager.Execute(request("process.start", map[string]any{
		"process_id": "pipe", "idempotency_key": "pipe-key",
		"command": "sleep 30", "cwd": root, "tty": false,
	}))
	if !started.OK {
		t.Fatal(started)
	}
	written := manager.Execute(request("process.input", map[string]any{
		"process_id": "pipe", "data": "secret\n",
	}))
	if written.OK || written.ErrorCode != "capability_unavailable" {
		t.Fatalf("pipe input should be rejected: %#v", written)
	}
	manager.Execute(request("process.cancel", map[string]any{"process_id": "pipe"}))
}

func TestInterruptIsDistinctFromTerminate(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	started := manager.Execute(request("process.start", map[string]any{
		"process_id": "interrupt", "idempotency_key": "interrupt-key",
		"command": "trap 'printf interrupted; exit 0' INT; while :; do sleep 1; done",
		"cwd":     root, "tty": false,
	}))
	if !started.OK {
		t.Fatal(started)
	}
	time.Sleep(50 * time.Millisecond)
	interrupted := manager.Execute(request("process.interrupt", map[string]any{
		"process_id": "interrupt",
	}))
	if !interrupted.OK {
		t.Fatal(interrupted)
	}
	result := waitDone(t, manager, "interrupt")
	if result.Data["termination_reason"] != "interrupted" {
		t.Fatalf("soft interrupt was not preserved as a distinct reason: %#v", result)
	}
	if result.Data["stdout_all"] != "interrupted" {
		t.Fatalf("process did not handle SIGINT: %#v", result)
	}
}

func TestConcurrentIdempotentStartExecutesCommandOnce(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	defer manager.Close()
	marker := filepath.Join(root, "marker")
	args := map[string]any{
		"process_id": "once", "idempotency_key": "once-key",
		"command": fmt.Sprintf("printf x >> %q", marker), "cwd": root, "tty": false,
	}
	const callers = 16
	results := make(chan protocol.WorkspaceResult, callers)
	var group sync.WaitGroup
	for index := 0; index < callers; index++ {
		group.Add(1)
		go func() {
			defer group.Done()
			results <- manager.Execute(request("process.start", args))
		}()
	}
	group.Wait()
	close(results)
	for result := range results {
		if !result.OK || result.Data["process_id"] != "once" {
			t.Fatalf("unexpected idempotent start result: %#v", result)
		}
	}
	waitDone(t, manager, "once")
	content, err := os.ReadFile(marker)
	if err != nil {
		t.Fatal(err)
	}
	if string(content) != "x" {
		t.Fatalf("command executed more than once: %q", content)
	}
}

func TestCloseReapsProcessStartedBeforeRegistration(t *testing.T) {
	root := t.TempDir()
	manager := NewManager(root, root)
	entered := make(chan struct{})
	release := make(chan struct{})
	var startedCmd *exec.Cmd
	manager.afterStart = func(cmd *exec.Cmd) {
		startedCmd = cmd
		close(entered)
		<-release
	}
	results := make(chan protocol.WorkspaceResult, 1)
	go func() {
		results <- manager.Execute(request("process.start", map[string]any{
			"process_id": "closing-start", "idempotency_key": "closing-start-key",
			"command": "sleep 30", "cwd": root,
		}))
	}()
	<-entered
	closed := make(chan struct{})
	go func() {
		manager.Close()
		close(closed)
	}()
	deadline := time.Now().Add(time.Second)
	for {
		manager.mu.Lock()
		closing := manager.closing
		manager.mu.Unlock()
		if closing {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("manager did not enter closing state")
		}
		time.Sleep(time.Millisecond)
	}
	close(release)

	result := <-results
	<-closed
	if result.OK || result.ErrorCode != "invalid_state" {
		t.Fatalf("start crossed closing boundary: %#v", result)
	}
	if startedCmd == nil || startedCmd.ProcessState == nil {
		t.Fatalf("unregistered process was not waited: %#v", startedCmd)
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
