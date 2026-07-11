package tools

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

// Execute retains only the protocol-v1 shell compatibility path. Filesystem
// product tools are implemented by the Host over generic workspace primitives.
func Execute(
	req protocol.ExecToolRequest,
	currentCWD string,
	onStream func(protocol.ToolStreamChunk),
) protocol.ExecToolResult {
	cwd := currentCWD
	staleWarning := ""
	if req.CWD != nil && *req.CWD != "" {
		cwd = *req.CWD
		if info, err := os.Stat(cwd); err != nil || !info.IsDir() {
			staleWarning = fmt.Sprintf(
				"Warning: working directory no longer exists (%s). Reset to %s.\n",
				cwd, currentCWD)
			cwd = currentCWD
		}
	}
	if req.ToolName != "shell" {
		return errorResult(
			"REMOTE_TOOL_ERROR",
			fmt.Sprintf("unsupported tool %q; use workspace primitives", req.ToolName),
		)
	}
	return prependWarning(runShell(req.Args, cwd, req.TimeoutSec, onStream), staleWarning)
}

func prependWarning(r protocol.ExecToolResult, warning string) protocol.ExecToolResult {
	if warning == "" || !r.OK {
		return r
	}
	r.Result = warning + r.Result
	return r
}

func runShell(
	args map[string]any,
	cwd string,
	timeoutSec int,
	onStream func(protocol.ToolStreamChunk),
) protocol.ExecToolResult {
	command, ok := args["command"].(string)
	if !ok || strings.TrimSpace(command) == "" {
		return errorResult("REMOTE_TOOL_ERROR", "shell command must be a non-empty string")
	}
	if timeout, ok := asInt(args["timeout"]); ok && timeout > 0 {
		timeoutSec = timeout
	}
	if timeoutSec <= 0 {
		timeoutSec = 120
	}

	ctx, cancel := context.WithTimeout(
		context.Background(), time.Duration(timeoutSec)*time.Second)
	defer cancel()

	shellName, shellArgs := pickShell(command)
	cmd := exec.CommandContext(ctx, shellName, shellArgs...)
	cmd.Dir = cwd
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if err := cmd.Start(); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}

	var stdoutBuf bytes.Buffer
	var stderrBuf bytes.Buffer
	var mu sync.Mutex
	var wg sync.WaitGroup
	readStream := func(reader io.Reader, kind string, target *bytes.Buffer) {
		defer wg.Done()
		buffer := make([]byte, 4096)
		for {
			n, readErr := reader.Read(buffer)
			if n > 0 {
				chunk := string(buffer[:n])
				mu.Lock()
				target.WriteString(chunk)
				mu.Unlock()
				if onStream != nil {
					onStream(protocol.ToolStreamChunk{ChunkType: kind, Data: chunk})
				}
			}
			if readErr != nil {
				return
			}
		}
	}
	wg.Add(2)
	go readStream(stdoutPipe, "stdout", &stdoutBuf)
	go readStream(stderrPipe, "stderr", &stderrBuf)
	err = cmd.Wait()
	wg.Wait()

	if ctx.Err() == context.DeadlineExceeded {
		return errorResult(
			"REMOTE_TIMEOUT",
			fmt.Sprintf("Remote execution timed out after %ds", timeoutSec),
		)
	}
	out := stdoutBuf.String()
	if stderrBuf.Len() > 0 {
		if out != "" {
			out += "\n"
		}
		out += "[stderr]\n" + stderrBuf.String()
	}
	exitCode := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
			if out != "" {
				out += "\n"
			}
			out += fmt.Sprintf("[exit code: %d]", exitCode)
		} else {
			return errorResult("REMOTE_TOOL_ERROR", err.Error())
		}
	}
	if strings.TrimSpace(out) == "" {
		out = "(no output)"
	}
	out = truncateOutput(out)
	return protocol.ExecToolResult{
		OK:     true,
		Result: out,
		Meta:   map[string]any{"exit_code": exitCode},
	}
}

func pickShell(command string) (string, []string) {
	if runtime.GOOS == "windows" {
		return "powershell", []string{"-Command", command}
	}
	return "sh", []string{"-lc", command}
}

const maxOutputChars = 15_000
const keepHeadChars = 6_000
const keepTailChars = 3_000

func truncateOutput(out string) string {
	if len(out) <= maxOutputChars {
		return out
	}
	return out[:keepHeadChars] +
		fmt.Sprintf("\n\n... truncated (%d chars total) ...\n\n", len(out)) +
		out[len(out)-keepTailChars:]
}

func errorResult(code, message string) protocol.ExecToolResult {
	return protocol.ExecToolResult{OK: false, ErrorCode: code, ErrorMessage: message}
}

func asInt(value any) (int, bool) {
	switch number := value.(type) {
	case float64:
		return int(number), true
	case int:
		return number, true
	default:
		return 0, false
	}
}
