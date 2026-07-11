package protocol

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"
)

type contractFixture struct {
	Name            string          `json:"name"`
	ProtocolVersion int             `json:"protocol_version"`
	Model           string          `json:"model"`
	Payload         json.RawMessage `json:"payload"`
}

func TestSharedContractFixturesRoundTripThroughGo(t *testing.T) {
	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate fixture test")
	}
	fixturePath := filepath.Join(
		filepath.Dir(filename), "..", "..", "..", "protocol", "fixtures", "remote_contract.json",
	)
	content, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatal(err)
	}
	var fixtures []contractFixture
	if err := json.Unmarshal(content, &fixtures); err != nil {
		t.Fatal(err)
	}
	for _, fixture := range fixtures {
		fixture := fixture
		t.Run(fixture.Name, func(t *testing.T) {
			decoded := fixtureModel(fixture.Model)
			if decoded == nil {
				t.Fatalf("unsupported fixture model %q", fixture.Model)
			}
			if err := json.Unmarshal(fixture.Payload, decoded); err != nil {
				t.Fatal(err)
			}
			encoded, err := json.Marshal(decoded)
			if err != nil {
				t.Fatal(err)
			}
			var expected any
			var actual any
			if err := json.Unmarshal(fixture.Payload, &expected); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(encoded, &actual); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(actual, expected) {
				t.Fatalf("contract drift:\nexpected: %s\nactual:   %s", fixture.Payload, encoded)
			}
		})
	}
}

func fixtureModel(name string) any {
	switch name {
	case "RegisterRequest":
		return &RegisterRequest{}
	case "TokenRefreshRequest":
		return &TokenRefreshRequest{}
	case "DisconnectRequest":
		return &DisconnectRequest{}
	case "InteractionReplyRequest":
		return &InteractionReplyRequest{}
	case "RelayEnvelope":
		return &RelayEnvelope{}
	case "WorkspaceRequest":
		return &WorkspaceRequest{}
	case "WorkspaceResult":
		return &WorkspaceResult{}
	case "ToolStreamChunk":
		return &ToolStreamChunk{}
	case "ExecToolRequest":
		return &ExecToolRequest{}
	default:
		return nil
	}
}
