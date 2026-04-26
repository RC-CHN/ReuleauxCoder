package tools

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

var skipDirs = map[string]struct{}{
	".git":         {},
	"node_modules": {},
	"__pycache__":  {},
	".venv":        {},
	"venv":         {},
	".tox":         {},
	"dist":         {},
	"build":        {},
}

func Execute(
	req protocol.ExecToolRequest,
	currentCWD string,
	onStream func(protocol.ToolStreamChunk),
) protocol.ExecToolResult {
	cwd := currentCWD
	if req.CWD != nil && *req.CWD != "" {
		cwd = *req.CWD
	}

	switch req.ToolName {
	case "shell":
		return runShell(req.Args, cwd, req.TimeoutSec, onStream)
	case "read_file":
		return readFile(req.Args, cwd)
	case "write_file":
		return writeFile(req.Args, cwd)
	case "edit_file":
		return editFile(req.Args, cwd)
	case "glob":
		return globFiles(req.Args, cwd)
	case "grep":
		return grepFiles(req.Args, cwd)
	default:
		return errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("unsupported tool %q", req.ToolName))
	}
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

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutSec)*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", "-lc", command)
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
	readStream := func(r io.Reader, kind string, target *bytes.Buffer) {
		defer wg.Done()
		buf := make([]byte, 4096)
		for {
			n, readErr := r.Read(buf)
			if n > 0 {
				chunk := string(buf[:n])
				mu.Lock()
				target.WriteString(chunk)
				mu.Unlock()
				if onStream != nil {
					onStream(protocol.ToolStreamChunk{ChunkType: kind, Data: chunk})
				}
			}
			if readErr != nil {
				if readErr == io.EOF {
					return
				}
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
		return errorResult("REMOTE_TIMEOUT", fmt.Sprintf("Remote execution timed out after %ds", timeoutSec))
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
	return protocol.ExecToolResult{OK: true, Result: out, Meta: map[string]any{"exit_code": exitCode}}
}

func readFile(args map[string]any, cwd string) protocol.ExecToolResult {
	filePath, ok := args["file_path"].(string)
	if !ok || filePath == "" {
		return errorResult("REMOTE_TOOL_ERROR", "file_path must be a non-empty string")
	}
	offset, _ := asInt(args["offset"])
	if offset <= 0 {
		offset = 1
	}
	limit, _ := asInt(args["limit"])
	if limit <= 0 {
		limit = 2000
	}
	override, _ := args["override"].(bool)

	resolved, err := resolvePath(cwd, filePath)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	data, err := os.ReadFile(resolved)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	text := strings.ReplaceAll(string(data), "\r\n", "\n")
	lines := strings.Split(text, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	if override {
		return protocol.ExecToolResult{OK: true, Result: joinNumbered(lines, 0)}
	}
	start := offset - 1
	if start >= len(lines) {
		return protocol.ExecToolResult{OK: true, Result: "(empty file)"}
	}
	end := start + limit
	if end > len(lines) {
		end = len(lines)
	}
	result := joinNumbered(lines[start:end], start)
	if result == "" {
		result = "(empty file)"
	}
	if end < len(lines) {
		result += fmt.Sprintf("\n... (%d lines total, showing %d-%d; use override=true to read full file)", len(lines), start+1, end)
	}
	return protocol.ExecToolResult{OK: true, Result: result}
}

func writeFile(args map[string]any, cwd string) protocol.ExecToolResult {
	filePath, ok := args["file_path"].(string)
	if !ok || filePath == "" {
		return errorResult("REMOTE_TOOL_ERROR", "file_path must be a non-empty string")
	}
	content, ok := args["content"].(string)
	if !ok {
		return errorResult("REMOTE_TOOL_ERROR", "content must be a string")
	}
	resolved, err := resolvePath(cwd, filePath)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if err := os.MkdirAll(filepath.Dir(resolved), 0o755); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if err := os.WriteFile(resolved, []byte(content), 0o644); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	lineCount := 0
	if content != "" {
		lineCount = strings.Count(content, "\n") + 1
	}
	return protocol.ExecToolResult{OK: true, Result: fmt.Sprintf("Wrote %d lines to %s", lineCount, filePath)}
}

func editFile(args map[string]any, cwd string) protocol.ExecToolResult {
	filePath, ok := args["file_path"].(string)
	if !ok || filePath == "" {
		return errorResult("REMOTE_TOOL_ERROR", "file_path must be a non-empty string")
	}
	oldString, ok := args["old_string"].(string)
	if !ok {
		return errorResult("REMOTE_TOOL_ERROR", "old_string must be a string")
	}
	newString, ok := args["new_string"].(string)
	if !ok {
		return errorResult("REMOTE_TOOL_ERROR", "new_string must be a string")
	}
	if oldString == newString {
		return errorResult("REMOTE_TOOL_ERROR", "old_string and new_string must differ")
	}
	resolved, err := resolvePath(cwd, filePath)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	data, err := os.ReadFile(resolved)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	content := string(data)
	count := strings.Count(content, oldString)
	if count == 0 {
		return errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("old_string not found in %s", filePath))
	}
	if count > 1 {
		return errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("old_string appears %d times in %s", count, filePath))
	}
	updated := strings.Replace(content, oldString, newString, 1)
	if err := os.WriteFile(resolved, []byte(updated), 0o644); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	return protocol.ExecToolResult{OK: true, Result: fmt.Sprintf("Edited %s", filePath)}
}

func globFiles(args map[string]any, cwd string) protocol.ExecToolResult {
	pattern, ok := args["pattern"].(string)
	if !ok || pattern == "" {
		return errorResult("REMOTE_TOOL_ERROR", "pattern must be a non-empty string")
	}
	pathValue, _ := args["path"].(string)
	if pathValue == "" {
		pathValue = "."
	}
	base, err := resolvePath(cwd, pathValue)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}

	hasGlobstar := strings.Contains(pattern, "**")
	var re *regexp.Regexp
	if hasGlobstar {
		re, err = compileGlobRegex(pattern)
		if err != nil {
			return errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("invalid glob pattern: %v", err))
		}
	}

	var matches []string
	walkErr := filepath.WalkDir(base, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if _, skip := skipDirs[d.Name()]; skip && path != base {
				return filepath.SkipDir
			}
			return nil
		}
		rel, err := filepath.Rel(base, path)
		if err != nil {
			return nil
		}
		// Normalize to forward slashes for consistent matching.
		relNorm := filepath.ToSlash(rel)

		if hasGlobstar {
			if re.MatchString(relNorm) {
				matches = append(matches, path)
			}
		} else {
			matched, err := filepath.Match(pattern, rel)
			if err == nil && matched {
				matches = append(matches, path)
			}
		}
		return nil
	})
	if walkErr != nil {
		return errorResult("REMOTE_TOOL_ERROR", walkErr.Error())
	}
	sort.Strings(matches)
	if len(matches) == 0 {
		return protocol.ExecToolResult{OK: true, Result: "No files matched."}
	}
	if len(matches) > 100 {
		matches = append(matches[:100], fmt.Sprintf("... (%d matches, showing first 100)", len(matches)))
	}
	return protocol.ExecToolResult{OK: true, Result: strings.Join(dedupe(matches), "\n")}
}

// compileGlobRegex converts a glob pattern with ** support into a compiled regex.
// ** matches zero or more path components; * matches within a single component;
// ? matches any single non-separator character.
func compileGlobRegex(pattern string) (*regexp.Regexp, error) {
	var buf strings.Builder
	buf.WriteString("^")
	for i := 0; i < len(pattern); i++ {
		c := pattern[i]
		switch {
		case c == '*' && i+1 < len(pattern) && pattern[i+1] == '*':
			buf.WriteString(".*")
			i++
		case c == '*':
			buf.WriteString("[^/]*")
		case c == '?':
			buf.WriteString("[^/]")
		case c == '.' || c == '+' || c == '(' || c == ')' || c == '|' ||
			c == '^' || c == '$' || c == '{' || c == '}' || c == '\\':
			buf.WriteByte('\\')
			buf.WriteByte(c)
		default:
			buf.WriteByte(c)
		}
	}
	buf.WriteString("$")
	return regexp.Compile(buf.String())
}

func grepFiles(args map[string]any, cwd string) protocol.ExecToolResult {
	pattern, ok := args["pattern"].(string)
	if !ok || pattern == "" {
		return errorResult("REMOTE_TOOL_ERROR", "pattern must be a non-empty string")
	}
	pathValue, _ := args["path"].(string)
	if pathValue == "" {
		pathValue = "."
	}
	include, _ := args["include"].(string)
	base, err := resolvePath(cwd, pathValue)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("Invalid regex: %v", err))
	}
	var files []string
	stat, err := os.Stat(base)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if stat.IsDir() {
		_ = filepath.WalkDir(base, func(path string, d os.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			if d.IsDir() {
				if _, skip := skipDirs[d.Name()]; skip && path != base {
					return filepath.SkipDir
				}
				return nil
			}
			if include != "" {
				matched, matchErr := filepath.Match(include, filepath.Base(path))
				if matchErr != nil || !matched {
					return nil
				}
			}
			files = append(files, path)
			return nil
		})
	} else {
		files = append(files, base)
	}
	var matches []string
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			continue
		}
		for idx, line := range strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n") {
			if re.MatchString(line) {
				matches = append(matches, fmt.Sprintf("%s:%d: %s", file, idx+1, line))
				if len(matches) >= 200 {
					matches = append(matches, "... (200 match limit reached)")
					return protocol.ExecToolResult{OK: true, Result: strings.Join(matches, "\n")}
				}
			}
		}
	}
	if len(matches) == 0 {
		return protocol.ExecToolResult{OK: true, Result: "No matches found."}
	}
	return protocol.ExecToolResult{OK: true, Result: strings.Join(matches, "\n")}
}

func resolvePath(cwd, path string) (string, error) {
	if filepath.IsAbs(path) {
		return filepath.Clean(path), nil
	}
	if cwd == "" {
		return filepath.Abs(path)
	}
	return filepath.Abs(filepath.Join(cwd, path))
}

func joinNumbered(lines []string, start int) string {
	if len(lines) == 0 {
		return "(empty file)"
	}
	parts := make([]string, 0, len(lines))
	for i, line := range lines {
		parts = append(parts, fmt.Sprintf("%d\t%s", start+i+1, line))
	}
	return strings.Join(parts, "\n")
}

func errorResult(code, message string) protocol.ExecToolResult {
	return protocol.ExecToolResult{OK: false, ErrorCode: code, ErrorMessage: message}
}

func asInt(v any) (int, bool) {
	// JSON numbers unmarshal into float64 from map[string]any.
	if n, ok := v.(float64); ok {
		return int(n), true
	}
	return 0, false
}

func dedupe(items []string) []string {
	seen := map[string]struct{}{}
	out := make([]string, 0, len(items))
	for _, item := range items {
		if _, ok := seen[item]; ok {
			continue
		}
		seen[item] = struct{}{}
		out = append(out, item)
	}
	return out
}
