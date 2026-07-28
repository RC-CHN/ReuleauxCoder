package tools

import (
	"bytes"
	"fmt"
	"strings"
	"sync/atomic"
	"time"

	processops "github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/process"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

var legacySequence atomic.Uint64

// Execute is a protocol-v1 adapter only. It translates the legacy shell tool
// envelope into the same process primitives used by protocol v2; it owns no
// subprocess, cancellation, timeout, or platform shell implementation.
func Execute(
	req protocol.ExecToolRequest,
	currentCWD string,
	manager *processops.Manager,
	onStream func(protocol.ToolStreamChunk),
) protocol.ExecToolResult {
	if req.ToolName != "shell" {
		return errorResult(
			"REMOTE_TOOL_ERROR",
			fmt.Sprintf("unsupported tool %q; use workspace primitives", req.ToolName),
		)
	}
	command, ok := req.Args["command"].(string)
	if !ok || strings.TrimSpace(command) == "" {
		return errorResult("REMOTE_TOOL_ERROR", "shell command must be a non-empty string")
	}
	timeoutSec := req.TimeoutSec
	if timeout, valid := asInt(req.Args["timeout"]); valid && timeout > 0 {
		timeoutSec = timeout
	}
	if timeoutSec <= 0 {
		timeoutSec = 120
	}
	cwd := currentCWD
	if req.CWD != nil && *req.CWD != "" {
		cwd = *req.CWD
	}

	sequence := legacySequence.Add(1)
	processID := fmt.Sprintf("legacy-shell-%d-%d", time.Now().UnixNano(), sequence)
	start := manager.Execute(protocol.WorkspaceRequest{
		Operation: "process.start",
		Args: map[string]any{
			"process_id":       processID,
			"idempotency_key":  processID,
			"command":          command,
			"cwd":              cwd,
			"deadline_unix_ms": time.Now().Add(time.Duration(timeoutSec) * time.Second).UnixMilli(),
		},
	})
	if !start.OK {
		return errorResult(start.ErrorCode, start.ErrorMessage)
	}

	var stdout bytes.Buffer
	var stderr bytes.Buffer
	stdoutOffset := 0
	stderrOffset := 0
	for {
		poll := manager.Execute(protocol.WorkspaceRequest{
			Operation: "process.poll",
			Args: map[string]any{
				"process_id":    processID,
				"stdout_offset": stdoutOffset,
				"stderr_offset": stderrOffset,
			},
		})
		if !poll.OK {
			return errorResult(poll.ErrorCode, poll.ErrorMessage)
		}
		stdoutChunk, _ := poll.Data["stdout"].(string)
		stderrChunk, _ := poll.Data["stderr"].(string)
		stdout.WriteString(stdoutChunk)
		stderr.WriteString(stderrChunk)
		if stdoutChunk != "" && onStream != nil {
			onStream(protocol.ToolStreamChunk{ChunkType: "stdout", Data: stdoutChunk})
		}
		if stderrChunk != "" && onStream != nil {
			onStream(protocol.ToolStreamChunk{ChunkType: "stderr", Data: stderrChunk})
		}
		stdoutOffset = asIntDefault(poll.Data["stdout_offset"])
		stderrOffset = asIntDefault(poll.Data["stderr_offset"])
		done, _ := poll.Data["done"].(bool)
		if !done {
			time.Sleep(20 * time.Millisecond)
			continue
		}
		if timedOut, _ := poll.Data["timed_out"].(bool); timedOut {
			return errorResult(
				"REMOTE_TIMEOUT",
				fmt.Sprintf("Remote execution timed out after %ds", timeoutSec),
			)
		}
		if cancelled, _ := poll.Data["cancelled"].(bool); cancelled {
			return errorResult("REMOTE_TOOL_ERROR", "shell command cancelled")
		}
		exitCode := asIntDefault(poll.Data["exit_code"])
		manager.Execute(protocol.WorkspaceRequest{
			Operation: "process.release",
			Args:      map[string]any{"process_id": processID},
		})
		return legacyShellResult(stdout.String(), stderr.String(), exitCode)
	}
}

func legacyShellResult(stdout, stderr string, exitCode int) protocol.ExecToolResult {
	result := stdout
	if stderr != "" {
		if result != "" {
			result += "\n"
		}
		result += "[stderr]\n" + stderr
	}
	if exitCode != 0 {
		if result != "" {
			result += "\n"
		}
		result += fmt.Sprintf("[exit code: %d]", exitCode)
	}
	if strings.TrimSpace(result) == "" {
		result = "(no output)"
	}
	return protocol.ExecToolResult{
		OK: true, Result: result, Meta: map[string]any{"exit_code": exitCode},
	}
}

func errorResult(code, message string) protocol.ExecToolResult {
	if code == "" {
		code = "REMOTE_TOOL_ERROR"
	}
	return protocol.ExecToolResult{OK: false, ErrorCode: code, ErrorMessage: message}
}

func asInt(value any) (int, bool) {
	switch number := value.(type) {
	case float64:
		return int(number), true
	case int:
		return number, true
	case int64:
		return int(number), true
	default:
		return 0, false
	}
}

func asIntDefault(value any) int {
	number, _ := asInt(value)
	return number
}
