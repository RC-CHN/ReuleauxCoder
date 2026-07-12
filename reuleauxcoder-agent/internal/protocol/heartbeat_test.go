package protocol

import (
	"encoding/json"
	"testing"
)

func TestHeartbeatCarriesOptionalTerminalResize(t *testing.T) {
	terminal := TerminalCapabilities{Width: 132, ColorLevel: "256", Unicode: true, Interactive: true}
	payload, err := json.Marshal(Heartbeat{PeerToken: "peer-token", TS: 1.5, Terminal: &terminal})
	if err != nil {
		t.Fatal(err)
	}

	var decoded Heartbeat
	if err := json.Unmarshal(payload, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Terminal == nil || decoded.Terminal.Width != 132 {
		t.Fatalf("terminal resize was not preserved: %#v", decoded.Terminal)
	}
}
