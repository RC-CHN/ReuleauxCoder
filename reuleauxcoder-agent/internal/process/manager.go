package process

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

type safeBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (b *safeBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.b.Write(p)
}

func (b *safeBuffer) snapshot() []byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]byte(nil), b.b.Bytes()...)
}

type state struct {
	id             string
	idempotencyKey string
	cmd            *exec.Cmd
	stdin          io.WriteCloser
	stdout         safeBuffer
	stderr         safeBuffer
	done           chan struct{}
	mu             sync.Mutex
	exitCode       int
	timedOut       bool
	cancelled      bool
	terminated     bool
}

type Manager struct {
	root       string
	defaultCWD string
	mu         sync.Mutex
	states     map[string]*state
	idempotent map[string]string
}

func NewManager(root, defaultCWD string) *Manager {
	return &Manager{
		root: root, defaultCWD: defaultCWD,
		states: make(map[string]*state), idempotent: make(map[string]string),
	}
}

func (m *Manager) Execute(req protocol.WorkspaceRequest) protocol.WorkspaceResult {
	switch req.Operation {
	case "process.start":
		return m.start(req.Args)
	case "process.poll":
		return m.poll(req.Args)
	case "process.input":
		return m.input(req.Args)
	case "process.cancel":
		return m.cancel(req.Args)
	default:
		return failure("invalid_path", fmt.Sprintf("unsupported process operation %q", req.Operation))
	}
}

func (m *Manager) start(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	idempotencyKey, _ := args["idempotency_key"].(string)
	command, _ := args["command"].(string)
	if processID == "" || idempotencyKey == "" || strings.TrimSpace(command) == "" {
		return failure("invalid_path", "process_id, idempotency_key and command are required")
	}
	m.mu.Lock()
	if existingID := m.idempotent[idempotencyKey]; existingID != "" {
		m.mu.Unlock()
		return success(map[string]any{"process_id": existingID, "reused": true})
	}
	m.mu.Unlock()

	cwd, _ := args["cwd"].(string)
	if cwd == "" {
		cwd = m.defaultCWD
	}
	resolvedCWD, err := confinedDirectory(m.root, cwd)
	if err != nil {
		return failure("path_outside_workspace", err.Error())
	}
	shell, shellArgs := shellCommand(command)
	cmd := exec.Command(shell, shellArgs...)
	cmd.Dir = resolvedCWD
	configureProcessGroup(cmd)
	stdinPipe, err := cmd.StdinPipe()
	if err != nil {
		return failure("io_error", err.Error())
	}
	processState := &state{
		id: processID, idempotencyKey: idempotencyKey,
		cmd: cmd, stdin: stdinPipe, done: make(chan struct{}),
	}
	// Let os/exec own the copy goroutines. Wait does not return until these
	// writers have consumed the pipes, which prevents a fast command from
	// exiting and closing StdoutPipe before our reader goroutine is scheduled.
	cmd.Stdout = &processState.stdout
	cmd.Stderr = &processState.stderr
	if err := cmd.Start(); err != nil {
		return failure("io_error", err.Error())
	}
	m.mu.Lock()
	if _, duplicate := m.states[processID]; duplicate {
		m.mu.Unlock()
		terminateProcessTree(cmd)
		return failure("not_unique", "process_id already exists")
	}
	m.states[processID] = processState
	m.idempotent[idempotencyKey] = processID
	m.mu.Unlock()

	go processState.wait()
	deadlineMillis := int64Arg(args["deadline_unix_ms"])
	if deadlineMillis > 0 {
		go func() {
			delay := time.Until(time.UnixMilli(deadlineMillis))
			if delay > 0 {
				timer := time.NewTimer(delay)
				defer timer.Stop()
				select {
				case <-timer.C:
					processState.terminate(true, false)
				case <-processState.done:
				}
			} else {
				processState.terminate(true, false)
			}
		}()
	}
	return success(map[string]any{"process_id": processID, "reused": false})
}

func (s *state) wait() {
	err := s.cmd.Wait()
	exitCode := 0
	if err != nil {
		if exitError, ok := err.(*exec.ExitError); ok {
			exitCode = exitError.ExitCode()
		} else {
			exitCode = -1
		}
	}
	s.mu.Lock()
	s.exitCode = exitCode
	s.mu.Unlock()
	close(s.done)
}

func (s *state) terminate(timedOut, cancelled bool) {
	select {
	case <-s.done:
		return
	default:
	}
	s.mu.Lock()
	if s.terminated {
		s.mu.Unlock()
		return
	}
	s.terminated = true
	s.timedOut = timedOut
	s.cancelled = cancelled
	s.mu.Unlock()
	terminateProcessTree(s.cmd)
}

func (m *Manager) poll(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	done := false
	select {
	case <-processState.done:
		done = true
	default:
	}
	// Observe completion before snapshotting output. Closing done happens only
	// after both pipe-copy goroutines finish, so a received close establishes
	// the happens-before edge needed for a complete terminal snapshot.
	stdout := processState.stdout.snapshot()
	stderr := processState.stderr.snapshot()
	stdoutOffset := boundedOffset(intArg(args["stdout_offset"]), len(stdout))
	stderrOffset := boundedOffset(intArg(args["stderr_offset"]), len(stderr))
	processState.mu.Lock()
	data := map[string]any{
		"process_id": processID,
		"stdout":     string(stdout[stdoutOffset:]), "stderr": string(stderr[stderrOffset:]),
		"stdout_offset": len(stdout), "stderr_offset": len(stderr), "done": done,
		"exit_code": processState.exitCode, "timed_out": processState.timedOut,
		"cancelled": processState.cancelled,
	}
	processState.mu.Unlock()
	if done {
		data["stdout_all"] = string(stdout)
		data["stderr_all"] = string(stderr)
		m.remove(processState)
	}
	return success(data)
}

func (m *Manager) cancel(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	processState.terminate(false, true)
	<-processState.done
	m.remove(processState)
	return success(map[string]any{"process_id": processID, "cancelled": true})
}

func (m *Manager) input(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	data, ok := args["data"].(string)
	if !ok {
		return failure("invalid_path", "data must be a string")
	}
	if data != "" {
		if _, err := io.WriteString(processState.stdin, data); err != nil {
			return failure("io_error", err.Error())
		}
	}
	closed, _ := args["close"].(bool)
	if closed {
		if err := processState.stdin.Close(); err != nil {
			return failure("io_error", err.Error())
		}
	}
	return success(map[string]any{
		"process_id": processID, "bytes_written": len(data), "closed": closed,
	})
}

func (m *Manager) lookup(processID string) *state {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.states[processID]
}

func (m *Manager) remove(processState *state) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.states[processState.id] == processState {
		delete(m.states, processState.id)
		delete(m.idempotent, processState.idempotencyKey)
	}
}

func (m *Manager) Close() {
	m.mu.Lock()
	states := make([]*state, 0, len(m.states))
	for _, processState := range m.states {
		states = append(states, processState)
	}
	m.mu.Unlock()
	for _, processState := range states {
		processState.terminate(false, true)
	}
}

func confinedDirectory(root, value string) (string, error) {
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	rootAbs, _ = filepath.EvalSymlinks(rootAbs)
	candidate := value
	if !filepath.IsAbs(candidate) {
		candidate = filepath.Join(rootAbs, candidate)
	}
	candidate, err = filepath.EvalSymlinks(candidate)
	if err != nil {
		return "", err
	}
	relative, err := filepath.Rel(rootAbs, candidate)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("cwd escapes workspace root: %s", value)
	}
	info, err := os.Stat(candidate)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("working directory does not exist: %s", value)
	}
	return candidate, nil
}

func shellCommand(command string) (string, []string) {
	if runtime.GOOS == "windows" {
		return "powershell", []string{"-Command", command}
	}
	return "sh", []string{"-lc", command}
}

func boundedOffset(value, length int) int {
	if value < 0 {
		return 0
	}
	if value > length {
		return length
	}
	return value
}

func intArg(value any) int {
	return int(int64Arg(value))
}

func int64Arg(value any) int64 {
	switch typed := value.(type) {
	case float64:
		return int64(typed)
	case int:
		return int64(typed)
	case int64:
		return typed
	default:
		return 0
	}
}

func success(data map[string]any) protocol.WorkspaceResult {
	return protocol.WorkspaceResult{OK: true, Data: data}
}

func failure(code, message string) protocol.WorkspaceResult {
	return protocol.WorkspaceResult{OK: false, ErrorCode: code, ErrorMessage: message}
}
