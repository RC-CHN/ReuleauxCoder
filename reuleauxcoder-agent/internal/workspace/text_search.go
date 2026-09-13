package workspace

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

type searchLimits struct {
	MaxFileBytes   int64   `json:"max_file_bytes"`
	MaxScanBytes   int64   `json:"max_scan_bytes"`
	MaxLineChars   int     `json:"max_line_chars"`
	MaxOutputChars int     `json:"max_output_chars"`
	TimeoutSec     float64 `json:"timeout_sec"`
}

var errSearchComplete = errors.New("search complete")

func searchText(root, pathValue string, args map[string]any) protocol.WorkspaceResult {
	pattern, ok := args["pattern"].(string)
	if !ok || pattern == "" {
		return failure("invalid_path", "pattern must be a non-empty string")
	}
	literal, _ := args["literal"].(bool)
	match := func(line string) bool { return strings.Contains(line, pattern) }
	if !literal {
		rx, err := regexp.Compile(pattern)
		if err != nil {
			return failure("invalid_path", "invalid regex: Go/RE2: "+err.Error())
		}
		match = rx.MatchString
	}
	// Defaults keep old hosts' literal search requests working. New hosts send all limits.
	limits := searchLimits{2 * 1024 * 1024, 32 * 1024 * 1024, 2000, 32000, 5}
	if value, exists := args["limits"]; exists {
		encoded, err := json.Marshal(value)
		if err != nil {
			return failure("invalid_path", err.Error())
		}
		if err := json.Unmarshal(encoded, &limits); err != nil {
			return failure("invalid_path", err.Error())
		}
	}
	maxFiles, maxMatches := intArg(args["max_files"], 5000), intArg(args["max_matches"], 200)
	if maxFiles < 1 || maxMatches < 1 || limits.MaxFileBytes < 1 || limits.MaxScanBytes < 1 || limits.MaxLineChars < 1 || limits.MaxOutputChars < 1 || limits.TimeoutSec <= 0 {
		return failure("invalid_path", "search limits must be positive")
	}
	include, _ := args["include"].(string)
	if strings.ContainsAny(include, "[]") {
		return failure("invalid_path", "remote include glob supports *, ? and **; character classes are not supported")
	}
	if include != "" && !strings.ContainsAny(include, "/\\") {
		include = "**/" + include
	}
	if include != "" {
		if _, err := portableGlobMatch("check", include); err != nil {
			return failure("invalid_path", "invalid include glob: "+err.Error())
		}
	}
	excluded := stringSetArg(args["exclude_dirs"])
	includeIgnored, _ := args["include_ignored"].(bool)
	info, err := os.Stat(root)
	if err != nil {
		return failure("io_error", err.Error())
	}
	if !info.IsDir() && !info.Mode().IsRegular() {
		return failure("not_a_file", pathValue+" is not searchable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(limits.TimeoutSec*float64(time.Second)))
	defer cancel()
	matches := make([]map[string]any, 0)
	reasons := map[string]bool{}
	scannedFiles, scannedBytes, outputChars, visited := 0, int64(0), 0, 0
	stop := func(reason string) error { reasons[reason] = true; return errSearchComplete }
	visit := func(full string, entry fs.FileInfo) error {
		if ctx.Err() != nil {
			return stop("timeout")
		}
		visited++
		if visited > maxFiles*4 {
			return stop("entry_limit")
		}
		if !entry.Mode().IsRegular() {
			return nil
		}
		if info.IsDir() {
			relative, err := filepath.Rel(root, full)
			if err != nil {
				return err
			}
			parts := strings.Split(filepath.ToSlash(relative), "/")
			for _, part := range parts[:len(parts)-1] {
				if excluded[part] {
					return nil
				}
			}
			if include != "" {
				matched, err := portableGlobMatch(filepath.ToSlash(relative), include)
				if err != nil {
					return err
				}
				if !matched {
					return nil
				}
			}
		}
		if scannedFiles >= maxFiles {
			return stop("file_limit")
		}
		scannedFiles++
		if entry.Size() > limits.MaxFileBytes {
			reasons["file_size"] = true
			return nil
		}
		file, err := os.Open(full)
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		defer file.Close()
		reader := bufio.NewReader(io.LimitReader(file, min(limits.MaxFileBytes, limits.MaxScanBytes-scannedBytes)+1))
		fileBytes, lineNumber := int64(0), 0
		for {
			if ctx.Err() != nil {
				return stop("timeout")
			}
			raw, readErr := reader.ReadString('\n')
			scannedBytes += int64(len(raw))
			fileBytes += int64(len(raw))
			if scannedBytes > limits.MaxScanBytes {
				return stop("scan_bytes")
			}
			if fileBytes > limits.MaxFileBytes {
				reasons["file_size"] = true
				return nil
			}
			if strings.ContainsRune(raw, 0) {
				return nil
			}
			for _, line := range pythonSplitlines(strings.ToValidUTF8(raw, "\ufffd")) {
				lineNumber++
				if !match(line) {
					continue
				}
				line = strings.TrimRightFunc(line, unicode.IsSpace)
				shortened := utf8.RuneCountInString(line) > limits.MaxLineChars
				if shortened {
					line = string([]rune(line)[:limits.MaxLineChars])
				}
				cost := utf8.RuneCountInString(full) + len(fmt.Sprint(lineNumber)) + utf8.RuneCountInString(line) + 4
				if shortened {
					cost += utf8.RuneCountInString(" … [line truncated]")
				}
				if outputChars+cost > limits.MaxOutputChars {
					return stop("output_chars")
				}
				outputChars += cost
				matches = append(matches, map[string]any{"path": full, "line_number": lineNumber, "line": line, "truncated": shortened})
				if shortened {
					reasons["line_chars"] = true
				}
				if len(matches) >= maxMatches {
					return stop("match_limit")
				}
			}
			if errors.Is(readErr, io.EOF) {
				return nil
			}
			if readErr != nil {
				return readErr
			}
		}
	}
	if info.IsDir() {
		err = visitSearchFiles(ctx, root, excluded, includeIgnored, visit)
	} else {
		err = visit(root, info)
	}
	if ctx.Err() != nil {
		reasons["timeout"] = true
	} else if err != nil && !errors.Is(err, errSearchComplete) {
		return failure("io_error", err.Error())
	}
	reasonList := make([]string, 0, len(reasons))
	for reason := range reasons {
		reasonList = append(reasonList, reason)
	}
	sort.Strings(reasonList)
	return success(map[string]any{"matches": matches, "truncated": len(reasons) > 0, "reasons": reasonList, "scanned_files": scannedFiles, "scanned_bytes": scannedBytes})
}

func visitSearchFiles(ctx context.Context, root string, excluded map[string]bool, includeIgnored bool, visit func(string, fs.FileInfo) error) error {
	git, gitErr := exec.LookPath("git")
	if !includeIgnored && gitErr == nil && insideGitRepository(ctx, git, root) {
		gitCtx, cancel := context.WithCancel(ctx)
		defer cancel()
		cmd := exec.CommandContext(gitCtx, git, "-c", "core.fsmonitor=false", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "--deduplicate", "-z", "--")
		pipe, err := cmd.StdoutPipe()
		if err != nil {
			return err
		}
		if err := cmd.Start(); err != nil {
			return err
		}
		reader := bufio.NewReader(pipe)
		for {
			name, readErr := reader.ReadString(0)
			if readErr != nil {
				waitErr := cmd.Wait()
				if !errors.Is(readErr, io.EOF) {
					return readErr
				}
				return waitErr
			}
			full := filepath.Join(root, strings.TrimSuffix(name, "\x00"))
			// Do not follow links in Git index paths, including linked parent directories.
			resolved, resolveErr := filepath.EvalSymlinks(full)
			if os.IsNotExist(resolveErr) {
				continue
			}
			if resolveErr != nil {
				cancel()
				cmd.Wait()
				return resolveErr
			}
			if resolved != full {
				continue
			}
			relative, relErr := filepath.Rel(root, resolved)
			if relErr != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
				continue
			}
			entry, statErr := os.Lstat(full)
			if os.IsNotExist(statErr) {
				continue
			}
			if statErr == nil {
				statErr = visit(full, entry)
			}
			if statErr != nil {
				cancel()
				cmd.Wait()
				return statErr
			}
		}
	}
	return filepath.WalkDir(root, func(full string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if entry.IsDir() {
			if full != root && excluded[entry.Name()] {
				return filepath.SkipDir
			}
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		return visit(full, info)
	})
}

func insideGitRepository(ctx context.Context, git, root string) bool {
	output, err := exec.CommandContext(ctx, git, "-C", root, "rev-parse", "--is-inside-work-tree").Output()
	return err == nil && strings.TrimSpace(string(output)) == "true"
}
