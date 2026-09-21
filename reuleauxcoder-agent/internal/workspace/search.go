package workspace

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func globEntries(root, pathValue string, args map[string]any) protocol.WorkspaceResult {
	info, err := os.Lstat(root)
	if err != nil {
		if os.IsNotExist(err) {
			return failure("not_found", fmt.Sprintf("%s not found", pathValue))
		}
		return failure("io_error", err.Error())
	}
	if !info.IsDir() {
		return failure("not_a_directory", fmt.Sprintf("%s is not a directory", pathValue))
	}
	pattern, ok := args["pattern"].(string)
	if !ok || strings.TrimSpace(pattern) == "" {
		return failure("invalid_path", "pattern must be a non-empty string")
	}
	if strings.ContainsAny(pattern, "\\[]") {
		return failure("invalid_path", "glob pattern requires compatibility fallback")
	}
	maxEntries := intArg(args["max_entries"], 20_000)
	maxMatches := intArg(args["max_matches"], 100)
	if maxEntries < 1 || maxMatches < 1 {
		return failure("invalid_path", "max_entries and max_matches must be positive")
	}

	hits := make([]map[string]any, 0)
	truncated, scanErr := scanWorkspace(root, maxEntries, func(full, relative string, entry os.DirEntry) error {
		matched, matchErr := portableGlobMatch(relative, pattern)
		if matchErr != nil {
			return matchErr
		}
		if !matched {
			return nil
		}
		entryInfo, infoErr := os.Lstat(full)
		if infoErr != nil {
			return nil
		}
		hits = append(hits, workspaceEntry(full, root, entryInfo))
		return nil
	}, func(relative string) bool {
		states, err := portableGlobStates(relative, pattern)
		if err != nil {
			return true
		}
		for _, state := range states[:len(states)-1] {
			if state {
				return true
			}
		}
		parts := splitPortablePath(pattern)
		return len(parts) > 0 && parts[len(parts)-1] == "**" && states[len(states)-1]
	})
	if scanErr != nil {
		return failure("io_error", scanErr.Error())
	}
	sort.SliceStable(hits, func(i, j int) bool {
		return hits[i]["mtime"].(float64) > hits[j]["mtime"].(float64)
	})
	total := len(hits)
	if len(hits) > maxMatches {
		hits = hits[:maxMatches]
	}
	return success(map[string]any{
		"entries":           hits,
		"match_count":       total,
		"listing_truncated": truncated,
	})
}

func scanWorkspace(root string, maxEntries int, visit func(string, string, os.DirEntry) error, descend func(string) bool) (bool, error) {
	type pendingDirectory struct {
		path   string
		prefix string
	}
	pending := []pendingDirectory{{path: root}}
	scanned := 0
	for len(pending) > 0 {
		last := len(pending) - 1
		directory := pending[last]
		pending = pending[:last]
		children, err := os.ReadDir(directory.path)
		if err != nil {
			return false, err
		}
		sort.Slice(children, func(i, j int) bool {
			return strings.ToLower(children[i].Name()) < strings.ToLower(children[j].Name())
		})
		for _, child := range children {
			relative := child.Name()
			if directory.prefix != "" {
				relative = filepath.Join(directory.prefix, child.Name())
			}
			full := filepath.Join(directory.path, child.Name())
			if err := visit(full, relative, child); err != nil {
				return false, err
			}
			scanned++
			if scanned >= maxEntries {
				return true, nil
			}
			if child.IsDir() && child.Type()&os.ModeSymlink == 0 && (descend == nil || descend(relative)) {
				pending = append(pending, pendingDirectory{path: full, prefix: relative})
			}
		}
	}
	return false, nil
}

func portableGlobMatch(relative, pattern string) (bool, error) {
	if len(splitPortablePath(relative)) == 0 || len(splitPortablePath(pattern)) == 0 {
		return false, nil
	}
	states, err := portableGlobStates(relative, pattern)
	if err != nil {
		return false, err
	}
	return states[len(states)-1], nil
}

func portableGlobStates(relative, pattern string) ([]bool, error) {
	pathParts := splitPortablePath(relative)
	patternParts := splitPortablePath(pattern)
	previous := make([]bool, len(patternParts)+1)
	previous[0] = true
	for index, segment := range patternParts {
		if segment == "**" {
			previous[index+1] = previous[index]
		}
	}
	for _, part := range pathParts {
		current := make([]bool, len(patternParts)+1)
		for index, segment := range patternParts {
			if segment == "**" {
				current[index+1] = current[index] || previous[index+1]
				continue
			}
			matched, err := path.Match(segment, part)
			if err != nil {
				return nil, err
			}
			current[index+1] = previous[index] && matched
		}
		previous = current
	}
	return previous, nil
}

func portableBasenameMatch(name, pattern string) bool {
	if runtime.GOOS == "windows" {
		name = strings.ToLower(name)
		pattern = strings.ToLower(pattern)
	}
	matched, err := path.Match(pattern, name)
	return err == nil && matched
}

func splitPortablePath(value string) []string {
	return strings.FieldsFunc(filepath.ToSlash(value), func(r rune) bool { return r == '/' })
}

func stringSetArg(value any) map[string]bool {
	result := map[string]bool{}
	items, _ := value.([]any)
	for _, item := range items {
		if text, ok := item.(string); ok {
			result[text] = true
		}
	}
	return result
}

func pythonSplitlines(value string) []string {
	lines := make([]string, 0)
	start := 0
	for index := 0; index < len(value); {
		r, size := utf8.DecodeRuneInString(value[index:])
		separator := r == '\n' || r == '\r' || r == '\v' || r == '\f' ||
			r == '\u001c' || r == '\u001d' || r == '\u001e' || r == '\u0085' ||
			r == '\u2028' || r == '\u2029'
		if !separator {
			index += size
			continue
		}
		lines = append(lines, value[start:index])
		index += size
		if r == '\r' && index < len(value) && value[index] == '\n' {
			index++
		}
		start = index
	}
	if start < len(value) {
		lines = append(lines, value[start:])
	}
	return lines
}
