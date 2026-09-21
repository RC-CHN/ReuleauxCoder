package workspace

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func readTextPage(path, display string, args map[string]any) protocol.WorkspaceResult {
	for _, key := range []string{"offset", "limit", "max_chars"} {
		if value, exists := args[key]; exists {
			parsed := intArg(value, 0)
			fraction, floating := value.(float64)
			if parsed < 1 || (floating && float64(parsed) != fraction) {
				return failure("invalid_path", "page limits must be positive integers")
			}
		}
	}
	offset, limit, maxChars := intArg(args["offset"], 1), intArg(args["limit"], 2000), intArg(args["max_chars"], 256*1024)
	if offset < 1 || limit < 1 || maxChars < 1 {
		return failure("invalid_path", "page limits must be positive integers")
	}
	info, err := os.Stat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return failure("not_found", fmt.Sprintf("%s not found", display))
		}
		return failure("io_error", err.Error())
	}
	if !info.Mode().IsRegular() {
		return failure("not_a_file", fmt.Sprintf("%s is not a file", display))
	}
	file, err := os.Open(path)
	if err != nil {
		return failure("io_error", err.Error())
	}
	defer file.Close()
	reader := bufio.NewReaderSize(file, 64*1024)
	lines := []string{}
	var current strings.Builder
	line, kept := 1, 0
	lineHasText, skipLF, selectedStarted := false, false, false
	result := func(total any, more, truncated bool) protocol.WorkspaceResult {
		return success(map[string]any{"lines": lines, "total_lines": total, "has_more": more, "truncated": truncated})
	}
	for {
		character, _, err := reader.ReadRune()
		if err == io.EOF {
			total := line - 1
			if lineHasText {
				total++
				if line >= offset {
					lines = append(lines, current.String())
				}
			}
			return result(total, false, false)
		}
		if err != nil {
			return failure("io_error", err.Error())
		}
		if skipLF && character == '\n' {
			skipLF = false
			continue
		}
		skipLF = false
		if line >= offset && line-offset >= limit {
			return result(nil, true, false)
		}
		ended := strings.ContainsRune("\n\r\v\f\x1c\x1d\x1e\u0085\u2028\u2029", character)
		skipLF = character == '\r'
		if !ended {
			lineHasText = true
		}
		if line >= offset {
			if !selectedStarted && len(lines) > 0 {
				kept++
				if kept > maxChars {
					return result(nil, true, true)
				}
			}
			selectedStarted = true
			if !ended {
				if kept >= maxChars {
					lines = append(lines, current.String())
					return result(nil, true, true)
				}
				current.WriteRune(character)
				kept++
			}
		}
		if ended {
			if line >= offset {
				lines = append(lines, current.String())
				current.Reset()
			}
			line++
			lineHasText, selectedStarted = false, false
		}
	}
}
