//go:build !windows

package tools

import (
	"strings"
	"testing"

	processops "github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/process"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestLegacyShellAdapterUsesProcessPrimitives(t *testing.T) {
	root := t.TempDir()
	manager := processops.NewManager(root, root)
	defer manager.Close()
	chunks := []protocol.ToolStreamChunk{}
	result := Execute(
		protocol.ExecToolRequest{
			ToolName:   "shell",
			Args:       map[string]any{"command": "printf legacy-ok"},
			TimeoutSec: 2,
		},
		root,
		manager,
		func(chunk protocol.ToolStreamChunk) { chunks = append(chunks, chunk) },
	)
	if !result.OK || !strings.Contains(result.Result, "legacy-ok") {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(chunks) == 0 || !strings.Contains(chunks[0].Data, "legacy-ok") {
		t.Fatalf("missing streamed primitive output: %#v", chunks)
	}
}

func TestLegacyAdapterRejectsProductTools(t *testing.T) {
	root := t.TempDir()
	manager := processops.NewManager(root, root)
	defer manager.Close()
	result := Execute(
		protocol.ExecToolRequest{ToolName: "write_file"},
		root,
		manager,
		nil,
	)
	if result.OK || !strings.Contains(result.ErrorMessage, "workspace primitives") {
		t.Fatalf("unexpected result: %#v", result)
	}
}
