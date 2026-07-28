package process

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

const (
	maxRetainedBytesPerStream = 512 * 1024
	maxPollBytesPerStream     = 64 * 1024
	maxProcessSessions        = 64
	terminalRetention         = 10 * time.Minute
)

type bufferSnapshot struct {
	data           []byte
	nextOffset     int64
	truncated      bool
	totalBytes     int64
	decodeReplaced bool
}

type boundedBuffer struct {
	mu          sync.Mutex
	data        []byte
	startOffset int64
	nextOffset  int64
	totalBytes  int64
	truncated   bool
	onChange    func()
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	b.data = append(b.data, p...)
	b.nextOffset += int64(len(p))
	b.totalBytes += int64(len(p))
	if len(b.data) > maxRetainedBytesPerStream {
		drop := len(b.data) - maxRetainedBytesPerStream
		retained := make([]byte, maxRetainedBytesPerStream)
		copy(retained, b.data[drop:])
		b.data = retained
		b.startOffset += int64(drop)
		b.truncated = true
	}
	notify := b.onChange
	b.mu.Unlock()
	if notify != nil {
		notify()
	}
	return len(p), nil
}

func (b *boundedBuffer) snapshot(offset int64, limit int, final bool) bufferSnapshot {
	b.mu.Lock()
	defer b.mu.Unlock()
	requested := offset
	if requested < b.startOffset {
		requested = b.startOffset
	}
	if requested > b.nextOffset {
		requested = b.nextOffset
	}
	start := int(requested - b.startOffset)
	truncated := offset < b.startOffset
	if truncated {
		for start < len(b.data) && !utf8.RuneStart(b.data[start]) {
			start++
			requested++
		}
	}
	available := b.data[start:]
	if len(available) > limit {
		available = available[:limit]
		truncated = true
	}
	consumable := consumableUTF8Prefix(available, final)
	available = available[:consumable]
	return bufferSnapshot{
		data:           append([]byte(nil), available...),
		nextOffset:     requested + int64(len(available)),
		truncated:      truncated,
		totalBytes:     b.totalBytes,
		decodeReplaced: !utf8.Valid(available),
	}
}

func consumableUTF8Prefix(data []byte, final bool) int {
	for index := 0; index < len(data); {
		if !utf8.FullRune(data[index:]) {
			if final {
				return len(data)
			}
			return index
		}
		_, size := utf8.DecodeRune(data[index:])
		index += size
	}
	return len(data)
}

func (b *boundedBuffer) endOffset() int64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.nextOffset
}

func (b *boundedBuffer) retained() []byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]byte(nil), b.data...)
}

type state struct {
	id                 string
	idempotencyKey     string
	cmd                *exec.Cmd
	stdin              io.WriteCloser
	stdout             boundedBuffer
	stderr             boundedBuffer
	done               chan struct{}
	changed            chan struct{}
	mu                 sync.Mutex
	exitCode           int
	terminationReason  string
	interruptRequested bool
	terminated         bool
	startedAt          time.Time
	finishedAt         time.Time
}

func (s *state) signalChange() {
	select {
	case s.changed <- struct{}{}:
	default:
	}
}

type Manager struct {
	root        string
	defaultCWD  string
	mu          sync.Mutex
	states      map[string]*state
	idempotent  map[string]string
	starting    map[string]*startReservation
	startingIDs map[string]struct{}
	closing     bool
}

type startReservation struct {
	done   chan struct{}
	result protocol.WorkspaceResult
}

func NewManager(root, defaultCWD string) *Manager {
	return &Manager{
		root: root, defaultCWD: defaultCWD,
		states:      make(map[string]*state),
		idempotent:  make(map[string]string),
		starting:    make(map[string]*startReservation),
		startingIDs: make(map[string]struct{}),
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
	case "process.interrupt":
		return m.interrupt(req.Args)
	case "process.terminate":
		return m.terminate(req.Args)
	case "process.release":
		return m.release(req.Args)
	case "process.cancel":
		return m.cancel(req.Args)
	default:
		return failure("invalid_path", fmt.Sprintf("unsupported process operation %q", req.Operation))
	}
}

func (m *Manager) start(args map[string]any) (result protocol.WorkspaceResult) {
	processID, _ := args["process_id"].(string)
	idempotencyKey, _ := args["idempotency_key"].(string)
	command, _ := args["command"].(string)
	if processID == "" || idempotencyKey == "" || command == "" {
		return failure("invalid_path", "process_id, idempotency_key and command are required")
	}
	if tty, _ := args["tty"].(bool); tty {
		return failure("capability_unavailable", "this peer does not advertise PTY process support")
	}

	m.mu.Lock()
	if m.closing {
		m.mu.Unlock()
		return failure("invalid_state", "process manager is shutting down")
	}
	m.cleanupTerminalLocked()
	if existingID := m.idempotent[idempotencyKey]; existingID != "" {
		m.mu.Unlock()
		return success(map[string]any{"process_id": existingID, "reused": true})
	}
	if reservation := m.starting[idempotencyKey]; reservation != nil {
		m.mu.Unlock()
		<-reservation.done
		return replayStartResult(reservation.result)
	}
	if _, duplicate := m.states[processID]; duplicate {
		m.mu.Unlock()
		return failure("not_unique", "process_id already exists")
	}
	if _, duplicate := m.startingIDs[processID]; duplicate {
		m.mu.Unlock()
		return failure("not_unique", "process_id is already starting")
	}
	if len(m.states)+len(m.starting) >= maxProcessSessions {
		m.mu.Unlock()
		return failure("resource_exhausted", fmt.Sprintf("process session capacity reached (%d)", maxProcessSessions))
	}
	reservation := &startReservation{done: make(chan struct{})}
	m.starting[idempotencyKey] = reservation
	m.startingIDs[processID] = struct{}{}
	m.mu.Unlock()
	defer func() {
		m.mu.Lock()
		reservation.result = result
		delete(m.starting, idempotencyKey)
		delete(m.startingIDs, processID)
		close(reservation.done)
		m.mu.Unlock()
	}()

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

	processState := &state{
		id: processID, idempotencyKey: idempotencyKey,
		cmd: cmd, done: make(chan struct{}), changed: make(chan struct{}, 1),
		startedAt: time.Now(),
	}
	processState.stdout.onChange = processState.signalChange
	processState.stderr.onChange = processState.signalChange

	// Missing tty is the protocol-v2 compatibility path used by older hosts.
	// New hosts always send tty=false, which deliberately closes stdin.
	if _, modeDeclared := args["tty"]; !modeDeclared {
		stdinPipe, pipeErr := cmd.StdinPipe()
		if pipeErr != nil {
			return failure("io_error", pipeErr.Error())
		}
		processState.stdin = stdinPipe
	}
	cmd.Stdout = &processState.stdout
	cmd.Stderr = &processState.stderr
	if err := cmd.Start(); err != nil {
		return failure("io_error", err.Error())
	}

	m.mu.Lock()
	if m.closing {
		m.mu.Unlock()
		terminateProcessTree(cmd)
		return failure("invalid_state", "process manager is shutting down")
	}
	if _, duplicate := m.states[processID]; duplicate {
		m.mu.Unlock()
		terminateProcessTree(cmd)
		return failure("not_unique", "process_id already exists")
	}
	if existingID := m.idempotent[idempotencyKey]; existingID != "" {
		m.mu.Unlock()
		terminateProcessTree(cmd)
		return success(map[string]any{"process_id": existingID, "reused": true})
	}
	m.states[processID] = processState
	m.idempotent[idempotencyKey] = processID
	m.mu.Unlock()

	go processState.wait()
	deadlineMillis := int64Arg(args["deadline_unix_ms"])
	if deadlineMillis > 0 {
		go func() {
			delay := time.Until(time.UnixMilli(deadlineMillis))
			if delay <= 0 {
				processState.terminate("timeout")
				return
			}
			timer := time.NewTimer(delay)
			defer timer.Stop()
			select {
			case <-timer.C:
				processState.terminate("timeout")
			case <-processState.done:
			}
		}()
	}
	return success(map[string]any{
		"process_id": processID, "reused": false,
		"started_unix_ms": processState.startedAt.UnixMilli(),
	})
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
	if s.terminationReason == "" {
		if s.interruptRequested {
			s.terminationReason = "interrupted"
		} else {
			s.terminationReason = "exit"
		}
	}
	s.finishedAt = time.Now()
	s.mu.Unlock()
	close(s.done)
	s.signalChange()
}

func (s *state) terminate(reason string) {
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
	if s.terminationReason == "" || reason == "timeout" || reason == "shutdown" {
		s.terminationReason = reason
	}
	s.mu.Unlock()
	terminateProcessTree(s.cmd)
}

func (m *Manager) poll(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	stdoutOffset := int64Arg(args["stdout_offset"])
	stderrOffset := int64Arg(args["stderr_offset"])
	waitMillis := int64Arg(args["wait_ms"])
	if waitMillis > 0 &&
		stdoutOffset >= processState.stdout.endOffset() &&
		stderrOffset >= processState.stderr.endOffset() &&
		!isDone(processState.done) {
		timer := time.NewTimer(time.Duration(waitMillis) * time.Millisecond)
		select {
		case <-processState.changed:
		case <-processState.done:
		case <-timer.C:
		}
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
	}

	done := isDone(processState.done)
	stdout := processState.stdout.snapshot(stdoutOffset, maxPollBytesPerStream, done)
	stderr := processState.stderr.snapshot(stderrOffset, maxPollBytesPerStream, done)
	processState.mu.Lock()
	stateName := "running"
	if done {
		stateName = "exited"
	}
	data := map[string]any{
		"process_id": processID,
		"state":      stateName,
		"stdout":     string(stdout.data), "stderr": string(stderr.data),
		"stdout_offset": stdout.nextOffset, "stderr_offset": stderr.nextOffset,
		"total_stdout_bytes": stdout.totalBytes, "total_stderr_bytes": stderr.totalBytes,
		"output_truncated":       stdout.truncated || stderr.truncated,
		"output_decode_replaced": stdout.decodeReplaced || stderr.decodeReplaced,
		"done":                   done, "exit_code": processState.exitCode,
		"termination_reason": processState.terminationReason,
		"timed_out":          processState.terminationReason == "timeout",
		"cancelled":          processState.terminationReason == "cancelled",
		"started_unix_ms":    processState.startedAt.UnixMilli(),
	}
	if !processState.finishedAt.IsZero() {
		data["finished_unix_ms"] = processState.finishedAt.UnixMilli()
	}
	processState.mu.Unlock()
	if done {
		// Retained for explicit process.release so a lost terminal response can
		// be polled again after transport recovery.
		data["stdout_all"] = string(processState.stdout.retained())
		data["stderr_all"] = string(processState.stderr.retained())
	}
	return success(data)
}

func (m *Manager) input(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	if processState.stdin == nil {
		return failure("capability_unavailable", "stdin is closed for this pipe-mode process")
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

func (m *Manager) interrupt(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	if isDone(processState.done) {
		return success(m.controlSnapshot(processState))
	}
	if err := interruptProcessTree(processState.cmd); err != nil {
		return failure("io_error", err.Error())
	}
	processState.mu.Lock()
	processState.interruptRequested = true
	processState.mu.Unlock()
	return success(m.controlSnapshot(processState))
}

func (m *Manager) terminate(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	reason, _ := args["reason"].(string)
	if reason == "" {
		reason = "terminated"
	}
	processState.terminate(reason)
	return success(m.controlSnapshot(processState))
}

func (m *Manager) cancel(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	processState.terminate("cancelled")
	<-processState.done
	data := m.controlSnapshot(processState)
	data["cancelled"] = true
	m.remove(processState)
	return success(data)
}

func (m *Manager) release(args map[string]any) protocol.WorkspaceResult {
	processID, _ := args["process_id"].(string)
	processState := m.lookup(processID)
	if processState == nil {
		return failure("not_found", "process not found")
	}
	if !isDone(processState.done) {
		return failure("invalid_state", "cannot release a running process")
	}
	m.remove(processState)
	return success(map[string]any{"process_id": processID, "released": true})
}

func (m *Manager) controlSnapshot(processState *state) map[string]any {
	done := isDone(processState.done)
	processState.mu.Lock()
	defer processState.mu.Unlock()
	stateName := "running"
	if done {
		stateName = "exited"
	}
	return map[string]any{
		"process_id":         processState.id,
		"state":              stateName,
		"done":               done,
		"exit_code":          processState.exitCode,
		"termination_reason": processState.terminationReason,
	}
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

func (m *Manager) cleanupTerminalLocked() {
	now := time.Now()
	for _, processState := range m.states {
		processState.mu.Lock()
		finishedAt := processState.finishedAt
		processState.mu.Unlock()
		if !finishedAt.IsZero() && now.Sub(finishedAt) >= terminalRetention {
			delete(m.states, processState.id)
			delete(m.idempotent, processState.idempotencyKey)
		}
	}
}

func (m *Manager) Close() {
	m.mu.Lock()
	m.closing = true
	states := make([]*state, 0, len(m.states))
	for _, processState := range m.states {
		states = append(states, processState)
	}
	m.mu.Unlock()
	for _, processState := range states {
		processState.terminate("shutdown")
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
		return "powershell", []string{"-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command}
	}
	return "sh", []string{"-c", command}
}

func isDone(done <-chan struct{}) bool {
	select {
	case <-done:
		return true
	default:
		return false
	}
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

func replayStartResult(result protocol.WorkspaceResult) protocol.WorkspaceResult {
	if !result.OK {
		return result
	}
	data := make(map[string]any, len(result.Data)+1)
	for key, value := range result.Data {
		data[key] = value
	}
	data["reused"] = true
	return success(data)
}

func success(data map[string]any) protocol.WorkspaceResult {
	return protocol.WorkspaceResult{OK: true, Data: data}
}

func failure(code, message string) protocol.WorkspaceResult {
	return protocol.WorkspaceResult{OK: false, ErrorCode: code, ErrorMessage: message}
}
