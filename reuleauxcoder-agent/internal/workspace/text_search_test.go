package workspace

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestBoundedRegexSearch(t *testing.T) {
	root := t.TempDir()
	for name, content := range map[string]string{
		"a.bin": "\x00needle",
		"b.map": strings.Repeat("needle", 100),
		"c.txt": "class Agent:\nclass Tool:\n",
	} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	result := searchText(root, ".", map[string]any{
		"pattern": `class\s+(Agent|Tool)`,
		"limits":  map[string]any{"max_file_bytes": 100, "max_line_chars": 7},
	})
	if !result.OK {
		t.Fatal(result)
	}
	matches := result.Data["matches"].([]map[string]any)
	if len(matches) != 2 || matches[0]["line"] != "class A" || matches[0]["truncated"] != true {
		t.Fatalf("unexpected matches: %#v", matches)
	}
	if strings.Join(result.Data["reasons"].([]string), ",") != "file_size,line_chars" {
		t.Fatal(result.Data)
	}
	if result.Data["scanned_bytes"].(int64) >= 100 {
		t.Fatal("read oversized input", result.Data)
	}
}

func TestSearchGitIgnoresAndExcludedDirectories(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git unavailable")
	}
	root := t.TempDir()
	if output, err := exec.Command("git", "init", "-q", root).CombinedOutput(); err != nil {
		t.Fatalf("%v: %s", err, output)
	}
	for name, content := range map[string]string{
		".gitignore": "generated/\n",
		"main.py":    "needle\n", "generated/out.py": "needle\n", "vendor/out.py": "needle\n",
	} {
		full := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(full), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	args := map[string]any{"pattern": "needle", "include": "*.py", "exclude_dirs": []any{".git", "vendor"}}
	result := searchText(root, ".", args)
	if !result.OK || len(result.Data["matches"].([]map[string]any)) != 1 {
		t.Fatal(result)
	}
	args["include_ignored"] = true
	result = searchText(root, ".", args)
	if !result.OK || len(result.Data["matches"].([]map[string]any)) != 2 {
		t.Fatal(result)
	}
}

func TestSearchLimitsStopReadingAndUnsupportedRegexIsExplicit(t *testing.T) {
	root := t.TempDir()
	file := filepath.Join(root, "text")
	if err := os.WriteFile(file, []byte(strings.Repeat("needle\n", 100)), 0600); err != nil {
		t.Fatal(err)
	}
	for _, limit := range []string{"max_scan_bytes", "max_output_chars"} {
		result := searchText(file, "text", map[string]any{"pattern": "needle", "limits": map[string]any{limit: 14}})
		if !result.OK || result.Data["truncated"] != true || result.Data["scanned_bytes"].(int64) >= 700 {
			t.Fatal(result)
		}
	}
	result := searchText(file, "text", map[string]any{"pattern": `(?<=a)b`})
	if result.OK || !strings.Contains(result.ErrorMessage, "Go/RE2") {
		t.Fatal(result)
	}
	result = searchText(root, ".", map[string]any{"pattern": "needle", "include": "[!a]*"})
	if result.OK || !strings.Contains(result.ErrorMessage, "character classes") {
		t.Fatal(result)
	}
}

func BenchmarkSearchWorkspace(b *testing.B) {
	root := b.TempDir()
	data := []byte(strings.Repeat("class Example: pass\n", 200))
	for i := 0; i < 500; i++ {
		if err := os.WriteFile(filepath.Join(root, fmt.Sprintf("file-%04d.py", i)), data, 0600); err != nil {
			b.Fatal(err)
		}
	}
	b.SetBytes(int64(500 * len(data)))
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		result := searchText(root, ".", map[string]any{"pattern": `class\s+Missing`, "include": "*.py"})
		if !result.OK || result.Data["scanned_files"] != 500 {
			b.Fatal(result)
		}
	}
}
