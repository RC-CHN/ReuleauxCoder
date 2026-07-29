package workspace

import (
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func Execute(req protocol.WorkspaceRequest, root, defaultCWD string) protocol.WorkspaceResult {
	if req.Operation != "fs.read_text" && req.Operation != "fs.snapshot_text" && req.Operation != "fs.write_text_atomic" && req.Operation != "fs.write_text_verified" && req.Operation != "fs.replace_exact_atomic" && req.Operation != "fs.replace_exact_verified" && req.Operation != "fs.stat" && req.Operation != "fs.list" && req.Operation != "fs.glob" && req.Operation != "fs.search_text" {
		return failure("invalid_path", fmt.Sprintf("unsupported workspace operation %q", req.Operation))
	}
	cwd := defaultCWD
	if req.CWD != nil && *req.CWD != "" {
		cwd = *req.CWD
	}
	pathValue, ok := req.Args["path"].(string)
	if !ok || strings.TrimSpace(pathValue) == "" {
		return failure("invalid_path", "path must be a non-empty string")
	}
	path, err := confinedPath(root, cwd, pathValue)
	if err != nil {
		return failure("path_outside_workspace", err.Error())
	}
	switch req.Operation {
	case "fs.stat":
		info, err := os.Lstat(path)
		if err != nil {
			if os.IsNotExist(err) {
				return failure("not_found", fmt.Sprintf("%s not found", pathValue))
			}
			return failure("io_error", err.Error())
		}
		return success(map[string]any{"entry": workspaceEntry(path, filepath.Dir(path), info)})
	case "fs.list":
		return listEntries(path, pathValue, req.Args)
	case "fs.glob":
		return globEntries(path, pathValue, req.Args)
	case "fs.search_text":
		return searchText(path, pathValue, req.Args)
	case "fs.read_text":
		content, err := os.ReadFile(path)
		if err != nil {
			if os.IsNotExist(err) {
				return failure("not_found", fmt.Sprintf("%s not found", pathValue))
			}
			if info, statErr := os.Stat(path); statErr == nil && info.IsDir() {
				return failure("not_a_file", fmt.Sprintf("%s is not a file", pathValue))
			}
			return failure("io_error", err.Error())
		}
		return success(map[string]any{"content": string(content)})
	case "fs.snapshot_text":
		content, revision, err := readDocument(path)
		if err != nil {
			if info, statErr := os.Stat(path); statErr == nil && info.IsDir() {
				return failure("not_a_file", fmt.Sprintf("%s is not a file", pathValue))
			}
			return failure("io_error", err.Error())
		}
		var text any
		if revision["exists"] == true {
			text = string(content)
		}
		return success(map[string]any{
			"resolved_path": path,
			"content":       text,
			"revision":      revision,
		})
	case "fs.write_text_atomic":
		content, ok := req.Args["content"].(string)
		if !ok {
			return failure("invalid_path", "content must be a string")
		}
		if info, statErr := os.Stat(path); statErr == nil && !info.Mode().IsRegular() {
			return failure("not_a_file", fmt.Sprintf("%s is not a file", pathValue))
		}
		old, _ := os.ReadFile(path)
		if err := atomicWrite(path, []byte(content)); err != nil {
			return failure("io_error", err.Error())
		}
		return success(map[string]any{"old_content": string(old)})
	case "fs.write_text_verified":
		content, ok := req.Args["content"].(string)
		if !ok {
			return failure("invalid_path", "content must be a string")
		}
		if info, statErr := os.Stat(path); statErr == nil && !info.Mode().IsRegular() {
			return failure("not_a_file", fmt.Sprintf("%s is not a file", pathValue))
		}
		beforeContent, before, readErr := readDocument(path)
		if readErr != nil {
			return failure("io_error", readErr.Error())
		}
		expected, _ := req.Args["expected_revision"].(map[string]any)
		writeErr := atomicWrite(path, []byte(content))
		_, observed, observeErr := readDocument(path)
		if observeErr != nil {
			observed = nil
		}
		receipt := mutationReceipt(
			path,
			before,
			expected,
			[]byte(content),
			observed,
			writeErr == nil,
			writeErr != nil,
		)
		data := map[string]any{
			"old_content":      nullableContent(beforeContent, before),
			"new_content":      content,
			"mutation_receipt": receipt,
		}
		if writeErr != nil {
			return failureWithData("io_error", writeErr.Error(), data)
		}
		return success(data)
	case "fs.replace_exact_atomic":
		old, oldOK := req.Args["old"].(string)
		newValue, newOK := req.Args["new"].(string)
		if !oldOK || !newOK || old == newValue {
			return failure("invalid_path", "old and new must be different strings")
		}
		content, err := os.ReadFile(path)
		if err != nil {
			if os.IsNotExist(err) {
				return failure("not_found", fmt.Sprintf("%s not found", pathValue))
			}
			return failure("io_error", err.Error())
		}
		count := strings.Count(string(content), old)
		if count == 0 {
			return failure("not_found", "old text was not found")
		}
		if count > 1 {
			return failure("not_unique", fmt.Sprintf("old text occurs %d times", count))
		}
		updated := strings.Replace(string(content), old, newValue, 1)
		if err := atomicWrite(path, []byte(updated)); err != nil {
			return failure("io_error", err.Error())
		}
		return success(map[string]any{
			"old_content": string(content),
			"new_content": updated,
		})
	case "fs.replace_exact_verified":
		old, oldOK := req.Args["old"].(string)
		newValue, newOK := req.Args["new"].(string)
		if !oldOK || !newOK || old == newValue {
			return failure("invalid_path", "old and new must be different strings")
		}
		content, before, readErr := readDocument(path)
		if readErr != nil {
			return failure("io_error", readErr.Error())
		}
		if before["exists"] != true {
			return failure("not_found", fmt.Sprintf("%s not found", pathValue))
		}
		count := strings.Count(string(content), old)
		if count == 0 {
			return failure("not_found", "old text was not found")
		}
		if count > 1 {
			return failure("not_unique", fmt.Sprintf("old text occurs %d times", count))
		}
		updated := strings.Replace(string(content), old, newValue, 1)
		expected, _ := req.Args["expected_revision"].(map[string]any)
		writeErr := atomicWrite(path, []byte(updated))
		_, observed, observeErr := readDocument(path)
		if observeErr != nil {
			observed = nil
		}
		receipt := mutationReceipt(
			path,
			before,
			expected,
			[]byte(updated),
			observed,
			writeErr == nil,
			writeErr != nil,
		)
		data := map[string]any{
			"old_content":      string(content),
			"new_content":      updated,
			"mutation_receipt": receipt,
		}
		if writeErr != nil {
			return failureWithData("io_error", writeErr.Error(), data)
		}
		return success(data)
	}
	return failure("invalid_path", fmt.Sprintf("unsupported workspace operation %q", req.Operation))
}

func readDocument(path string) ([]byte, map[string]any, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, missingRevision(), nil
		}
		return nil, nil, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return nil, nil, err
	}
	sum := sha256.Sum256(content)
	return content, map[string]any{
		"exists":        true,
		"sha256":        fmt.Sprintf("%x", sum),
		"size_bytes":    len(content),
		"mtime_ns":      info.ModTime().UnixNano(),
		"authoritative": true,
	}, nil
}

func missingRevision() map[string]any {
	return map[string]any{
		"exists":        false,
		"sha256":        nil,
		"size_bytes":    0,
		"mtime_ns":      nil,
		"authoritative": true,
	}
}

func nullableContent(content []byte, revision map[string]any) any {
	if revision["exists"] != true {
		return nil
	}
	return string(content)
}

func mutationReceipt(
	path string,
	before map[string]any,
	expected map[string]any,
	intended []byte,
	observed map[string]any,
	atomicReplace bool,
	writeFailed bool,
) map[string]any {
	sum := sha256.Sum256(intended)
	intendedHash := fmt.Sprintf("%x", sum)
	verification := "unknown"
	if observed != nil {
		intendedRevision := map[string]any{
			"exists":     true,
			"sha256":     intendedHash,
			"size_bytes": len(intended),
		}
		switch {
		case sameRevision(observed, intendedRevision):
			verification = "applied_verified"
		case writeFailed && sameRevision(observed, before):
			verification = "failed_unchanged"
		default:
			verification = "diverged"
		}
	}
	var expectedValue any
	if expected != nil {
		expectedValue = expected
	}
	var observedValue any
	if observed != nil {
		observedValue = observed
	}
	return map[string]any{
		"resolved_path":                path,
		"before":                       before,
		"intended_after_sha256":        intendedHash,
		"intended_size_bytes":          len(intended),
		"observed_after":               observedValue,
		"atomic_replace":               atomicReplace,
		"verification":                 verification,
		"expected_before":              expectedValue,
		"external_change_before_write": expected != nil && !sameRevision(expected, before),
	}
}

func sameRevision(left, right map[string]any) bool {
	return boolArg(left["exists"]) == boolArg(right["exists"]) &&
		stringArg(left["sha256"]) == stringArg(right["sha256"]) &&
		int64Arg(left["size_bytes"]) == int64Arg(right["size_bytes"])
}

func boolArg(value any) bool {
	typed, _ := value.(bool)
	return typed
}

func stringArg(value any) string {
	typed, _ := value.(string)
	return typed
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

func listEntries(path, pathValue string, args map[string]any) protocol.WorkspaceResult {
	info, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return failure("not_found", fmt.Sprintf("%s not found", pathValue))
		}
		return failure("io_error", err.Error())
	}
	if !info.IsDir() {
		if info.Mode().IsRegular() {
			return success(map[string]any{
				"entries":   []map[string]any{workspaceEntry(path, filepath.Dir(path), info)},
				"truncated": false,
			})
		}
		return failure("not_a_directory", fmt.Sprintf("%s is not a directory", pathValue))
	}
	recursive, _ := args["recursive"].(bool)
	includeHidden := true
	if value, ok := args["include_hidden"].(bool); ok {
		includeHidden = value
	}
	maxEntries := intArg(args["max_entries"], 10_000)
	if maxEntries < 1 {
		return failure("invalid_path", "max_entries must be positive")
	}

	entries := make([]map[string]any, 0)
	pending := []string{path}
	truncated := false
	for len(pending) > 0 && !truncated {
		directory := pending[len(pending)-1]
		pending = pending[:len(pending)-1]
		children, readErr := os.ReadDir(directory)
		if readErr != nil {
			return failure("io_error", readErr.Error())
		}
		sort.Slice(children, func(i, j int) bool {
			return strings.ToLower(children[i].Name()) < strings.ToLower(children[j].Name())
		})
		for _, child := range children {
			if !includeHidden && strings.HasPrefix(child.Name(), ".") {
				continue
			}
			childPath := filepath.Join(directory, child.Name())
			childInfo, infoErr := os.Lstat(childPath)
			if infoErr != nil {
				continue
			}
			entries = append(entries, workspaceEntry(childPath, path, childInfo))
			if len(entries) >= maxEntries {
				truncated = true
				break
			}
			if recursive && childInfo.IsDir() && childInfo.Mode()&os.ModeSymlink == 0 {
				pending = append(pending, childPath)
			}
		}
	}
	return success(map[string]any{"entries": entries, "truncated": truncated})
}

func workspaceEntry(path, base string, info os.FileInfo) map[string]any {
	relative, err := filepath.Rel(base, path)
	if err != nil {
		relative = info.Name()
	}
	mode := int64(info.Mode().Perm())
	if info.IsDir() {
		mode |= 0o040000
	} else if info.Mode().IsRegular() {
		mode |= 0o100000
	}
	return map[string]any{
		"path":          path,
		"relative_path": filepath.ToSlash(relative),
		"name":          info.Name(),
		"is_file":       info.Mode().IsRegular(),
		"is_dir":        info.IsDir(),
		"size":          info.Size(),
		"mtime":         float64(info.ModTime().UnixNano()) / 1e9,
		"mode":          mode,
	}
}

func intArg(value any, fallback int) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	case int64:
		return int(typed)
	default:
		return fallback
	}
}

func confinedPath(root, cwd, value string) (string, error) {
	if strings.TrimSpace(value) == "" {
		return "", fmt.Errorf("path must be non-empty")
	}
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	if evaluated, evalErr := filepath.EvalSymlinks(rootAbs); evalErr == nil {
		rootAbs = evaluated
	}
	cwdAbs, err := filepath.Abs(cwd)
	if err != nil {
		return "", err
	}
	if evaluated, evalErr := filepath.EvalSymlinks(cwdAbs); evalErr == nil {
		cwdAbs = evaluated
	}
	if rel, relErr := filepath.Rel(rootAbs, cwdAbs); relErr != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("cwd escapes workspace root: %s", cwd)
	}
	candidate := value
	if !filepath.IsAbs(candidate) {
		candidate = filepath.Join(cwdAbs, candidate)
	}
	candidateAbs, err := filepath.Abs(candidate)
	if err != nil {
		return "", err
	}
	parent := candidateAbs
	if _, err := os.Stat(candidateAbs); os.IsNotExist(err) {
		parent = filepath.Dir(candidateAbs)
	}
	if evaluated, err := filepath.EvalSymlinks(parent); err == nil {
		if parent == candidateAbs {
			candidateAbs = evaluated
		} else {
			candidateAbs = filepath.Join(evaluated, filepath.Base(candidateAbs))
		}
	}
	rel, err := filepath.Rel(rootAbs, candidateAbs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("path escapes workspace root: %s", value)
	}
	return candidateAbs, nil
}

func atomicWrite(path string, content []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".rcoder-*")
	if err != nil {
		return err
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if _, err := temporary.Write(content); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if info, err := os.Stat(path); err == nil {
		if err := os.Chmod(temporaryName, info.Mode()); err != nil {
			return err
		}
	}
	return os.Rename(temporaryName, path)
}

func success(data map[string]any) protocol.WorkspaceResult {
	return protocol.WorkspaceResult{OK: true, Data: data}
}

func failure(code, message string) protocol.WorkspaceResult {
	return protocol.WorkspaceResult{OK: false, ErrorCode: code, ErrorMessage: message}
}

func failureWithData(code, message string, data map[string]any) protocol.WorkspaceResult {
	return protocol.WorkspaceResult{
		OK:           false,
		Data:         data,
		ErrorCode:    code,
		ErrorMessage: message,
	}
}
