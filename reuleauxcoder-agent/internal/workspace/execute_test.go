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
