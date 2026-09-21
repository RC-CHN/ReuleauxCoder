package workspace

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestTextPageBoundaries(t *testing.T) {
	for _, separator := range []string{"\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\u0085", "\u2028", "\u2029"} {
		t.Run(separator, func(t *testing.T) {
			root := t.TempDir()
			text := strings.Join([]string{"alpha", "", "中😀", "omega"}, separator)
			if err := os.WriteFile(filepath.Join(root, "test.txt"), []byte(text), 0o600); err != nil {
				t.Fatal(err)
			}
			result := Execute(request("fs.read_text_page", map[string]any{"path": "test.txt", "offset": 2, "limit": 2}), root, root)
			if !result.OK || !reflect.DeepEqual(result.Data["lines"], []string{"", "中😀"}) || result.Data["has_more"] != true || result.Data["total_lines"] != nil {
				t.Fatalf("unexpected page: %#v", result)
			}
			result = Execute(request("fs.read_text_page", map[string]any{"path": "test.txt", "offset": 3, "limit": 2}), root, root)
			if !result.OK || !reflect.DeepEqual(result.Data["lines"], []string{"中😀", "omega"}) || result.Data["total_lines"] != 4 || result.Data["has_more"] != false {
				t.Fatalf("unexpected last page: %#v", result)
			}
		})
	}
}

func TestTextPageCharacterLimit(t *testing.T) {
	root := t.TempDir()
	for _, text := range []string{strings.Repeat("中😀", 100000), strings.Repeat("\n", 100000)} {
		if err := os.WriteFile(filepath.Join(root, "long.txt"), []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
		result := Execute(request("fs.read_text_page", map[string]any{"path": "long.txt", "limit": 100000, "max_chars": 20}), root, root)
		if !result.OK || result.Data["truncated"] != true || result.Data["has_more"] != true {
			t.Fatalf("unexpected truncation: %#v", result)
		}
		if len([]rune(strings.Join(result.Data["lines"].([]string), "\n"))) > 20 {
			t.Fatal("page exceeded character budget")
		}
	}
}
