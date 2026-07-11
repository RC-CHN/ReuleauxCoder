package workspace

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestExecuteWorkspacePrimitives(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "demo.txt")
	if err := os.WriteFile(path, []byte("alpha\nbeta\n"), 0o640); err != nil {
		t.Fatal(err)
	}

	read := Execute(request("fs.read_text", map[string]any{"path": "demo.txt"}), root, root)
	if !read.OK || read.Data["content"] != "alpha\nbeta\n" {
		t.Fatalf("unexpected read result: %#v", read)
	}

	replace := Execute(request("fs.replace_exact_atomic", map[string]any{
		"path": "demo.txt", "old": "beta", "new": "gamma",
	}), root, root)
	if !replace.OK || replace.Data["new_content"] != "alpha\ngamma\n" {
		t.Fatalf("unexpected replace result: %#v", replace)
	}

	write := Execute(request("fs.write_text_atomic", map[string]any{
		"path": "nested/new.txt", "content": "created",
	}), root, root)
	if !write.OK || write.Data["old_content"] != "" {
		t.Fatalf("unexpected write result: %#v", write)
	}
	content, err := os.ReadFile(filepath.Join(root, "nested", "new.txt"))
	if err != nil || string(content) != "created" {
		t.Fatalf("unexpected written file: %q, %v", content, err)
	}
}

func TestStructuredStatAndListPrimitives(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "nested"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "nested", "demo.py"), []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".hidden"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}

	statResult := Execute(request("fs.stat", map[string]any{"path": "nested/demo.py"}), root, root)
	if !statResult.OK {
		t.Fatalf("unexpected stat result: %#v", statResult)
	}
	entry := statResult.Data["entry"].(map[string]any)
	if entry["name"] != "demo.py" || entry["is_file"] != true {
		t.Fatalf("unexpected stat entry: %#v", entry)
	}

	listResult := Execute(request("fs.list", map[string]any{
		"path": ".", "recursive": true, "include_hidden": false, "max_entries": 10,
	}), root, root)
	if !listResult.OK {
		t.Fatalf("unexpected list result: %#v", listResult)
	}
	entries := listResult.Data["entries"].([]map[string]any)
	if len(entries) != 2 {
		t.Fatalf("expected directory and nested file, got %#v", entries)
	}
	for _, item := range entries {
		if item["name"] == ".hidden" {
			t.Fatalf("hidden entry leaked: %#v", entries)
		}
	}
}

func TestListPrimitiveIsBounded(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"a", "b", "c"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(name), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	result := Execute(request("fs.list", map[string]any{
		"path": ".", "max_entries": 2,
	}), root, root)
	if !result.OK || result.Data["truncated"] != true {
		t.Fatalf("expected truncated list, got %#v", result)
	}
	if len(result.Data["entries"].([]map[string]any)) != 2 {
		t.Fatalf("expected two entries, got %#v", result)
	}
}

func TestExecuteRejectsWorkspaceEscape(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(filepath.Dir(root), "outside.txt")
	result := Execute(request("fs.read_text", map[string]any{"path": outside}), root, root)
	if result.OK || result.ErrorCode != "path_outside_workspace" {
		t.Fatalf("expected confinement error, got %#v", result)
	}
}

func TestExecuteRejectsSymlinkEscape(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlink creation commonly requires elevated privileges on Windows")
	}
	root := t.TempDir()
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	result := Execute(
		request("fs.write_text_atomic", map[string]any{
			"path": "link/escaped.txt", "content": "nope",
		}),
		root,
		root,
	)
	if result.OK || result.ErrorCode != "path_outside_workspace" {
		t.Fatalf("expected symlink confinement error, got %#v", result)
	}
}

func TestExecuteUsesStableValidationErrors(t *testing.T) {
	root := t.TempDir()
	tests := []struct {
		name string
		req  protocol.WorkspaceRequest
		code string
	}{
		{"missing path", request("fs.read_text", map[string]any{}), "invalid_path"},
		{"unknown operation", request("fs.unknown", map[string]any{"path": "x"}), "invalid_path"},
		{"missing file", request("fs.read_text", map[string]any{"path": "missing"}), "not_found"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			result := Execute(test.req, root, root)
			if result.OK || result.ErrorCode != test.code {
				t.Fatalf("expected %s, got %#v", test.code, result)
			}
		})
	}
}

func request(operation string, args map[string]any) protocol.WorkspaceRequest {
	return protocol.WorkspaceRequest{Operation: operation, Args: args, TimeoutSec: 30}
}
