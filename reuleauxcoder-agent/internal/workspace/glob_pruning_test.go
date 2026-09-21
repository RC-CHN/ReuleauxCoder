package workspace

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestGlobSkipsImpossibleSubtrees(t *testing.T) {
	root := t.TempDir()
	for _, directory := range []string{"src", "node_modules", "src/nested"} {
		if err := os.MkdirAll(filepath.Join(root, directory), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 20; i++ {
		if err := os.WriteFile(filepath.Join(root, "node_modules", fmt.Sprintf("%d.py", i)), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"src/a.py", "src/nested/b.py"} {
		if err := os.WriteFile(filepath.Join(root, name), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	result := Execute(request("fs.glob", map[string]any{"path": ".", "pattern": "src/*.py", "max_entries": 5}), root, root)
	if !result.OK || result.Data["match_count"] != 1 || result.Data["listing_truncated"] != false {
		t.Fatalf("unexpected glob: %#v", result)
	}
	result = Execute(request("fs.glob", map[string]any{"path": ".", "pattern": "src/**/*.py", "max_entries": 10}), root, root)
	if !result.OK || result.Data["match_count"] != 2 || result.Data["listing_truncated"] != false {
		t.Fatalf("unexpected recursive glob: %#v", result)
	}
}
