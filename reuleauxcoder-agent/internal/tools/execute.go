package tools

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
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
	staleWarning := ""
	if req.CWD != nil && *req.CWD != "" {
		cwd = *req.CWD
		// Detect stale CWD and fall back to workspace root.
		if info, err := os.Stat(cwd); err != nil || !info.IsDir() {
			staleWarning = fmt.Sprintf(
				"Warning: working directory no longer exists (%s). Reset to %s.\n",
				cwd, currentCWD)
			cwd = currentCWD
		}
	}

	switch req.ToolName {
	case "shell":
		return prependWarning(runShell(req.Args, cwd, req.TimeoutSec, onStream), staleWarning)
	case "read_file":
		return prependWarning(readFile(req.Args, cwd), staleWarning)
	case "write_file":
		return prependWarning(writeFile(req.Args, cwd), staleWarning)
	case "edit_file":
		return prependWarning(editFile(req.Args, cwd), staleWarning)
	case "glob":
		return prependWarning(globFiles(req.Args, cwd), staleWarning)
	case "grep":
		return prependWarning(grepFiles(req.Args, cwd), staleWarning)
	default:
		return errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("unsupported tool %q", req.ToolName))
	}
}

// prependWarning prepends a warning message to the tool result if non-empty.
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

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutSec)*time.Second)
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
	out = truncateOutput(out)
	return protocol.ExecToolResult{OK: true, Result: out, Meta: map[string]any{"exit_code": exitCode}}
}

// pickShell returns the platform-appropriate shell and its argument template.
func pickShell(command string) (string, []string) {
	if runtime.GOOS == "windows" {
		return "powershell", []string{"-Command", command}
	}
	return "sh", []string{"-lc", command}
}

// truncateOutput truncates shell output exceeding maxOutputChars,
// preserving head and tail to keep both initial context and final error messages.
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
	f, err := os.Open(resolved)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	var lines []string
	lineNo := 0
	start := offset - 1
	end := start + limit

	for scanner.Scan() {
		lineNo++
		if override {
			lines = append(lines, scanner.Text())
			continue
		}
		if lineNo > start && lineNo <= end {
			lines = append(lines, scanner.Text())
		}
	}
	if err := scanner.Err(); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}

	if override {
		return protocol.ExecToolResult{OK: true, Result: joinNumbered(lines, 0)}
	}
	if start >= lineNo {
		return protocol.ExecToolResult{OK: true, Result: "(empty file)"}
	}
	result := joinNumbered(lines, start)
	if result == "" {
		result = "(empty file)"
	}
	if end < lineNo {
		result += fmt.Sprintf("\n... (%d lines total, showing %d-%d; use override=true to read full file)", lineNo, start+1, min(end, lineNo))
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
	return protocol.ExecToolResult{OK: true, Result: formatDiff(filePath, oldString, newString)}
}

// formatDiff produces a unified-diff-style summary of an edit, truncated at 3k chars.
func formatDiff(filePath string, oldStr, newStr string) string {
	var buf strings.Builder
	buf.WriteString(fmt.Sprintf("--- a/%s\n+++ b/%s\n", filePath, filePath))
	for _, line := range strings.Split(oldStr, "\n") {
		buf.WriteString("-" + line + "\n")
	}
	for _, line := range strings.Split(newStr, "\n") {
		buf.WriteString("+" + line + "\n")
	}
	out := buf.String()
	if len(out) > 3000 {
		out = out[:3000] + "\n... (diff truncated)"
	}
	return out
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
	sortByMtime(matches)
	if len(matches) == 0 {
		return protocol.ExecToolResult{OK: true, Result: "No files matched."}
	}
	if len(matches) > 100 {
		matches = append(matches[:100], fmt.Sprintf("... (%d matches, showing first 100)", len(matches)))
	}
	return protocol.ExecToolResult{OK: true, Result: strings.Join(dedupe(matches), "\n")}
}

// sortByMtime sorts file paths by modification time descending (newest first),
// matching Python's Path.glob() behavior. Files with stat errors sort to the end.
func sortByMtime(paths []string) {
	type entry struct {
		path  string
		mtime int64
	}
	entries := make([]entry, len(paths))
	for i, p := range paths {
		entries[i].path = p
		info, err := os.Stat(p)
		if err == nil {
			entries[i].mtime = info.ModTime().UnixNano()
		}
	}
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].mtime > entries[j].mtime
	})
	for i, e := range entries {
		paths[i] = e.path
	}
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
