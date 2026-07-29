package runner

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestIdleDoubleInterruptExitsInteractiveLoop(t *testing.T) {
	r := &Runner{
		lines:    make(chan string),
		inputErr: make(chan error, 1),
	}
	interrupts := make(chan os.Signal, 2)
	interrupts <- os.Interrupt
	interrupts <- os.Interrupt

	started := time.Now()
	if err := r.runInteractiveLoop(context.Background(), "unused", interrupts); err != nil {
		t.Fatal(err)
	}
	if time.Since(started) > time.Second {
		t.Fatal("idle double interrupt did not exit promptly")
	}
}

func TestParseGenericInteractionInput(t *testing.T) {
	tests := []struct {
		name      string
		payload   map[string]any
		input     string
		expected  any
		cancelled bool
		wantErr   bool
	}{
		{
			name: "boolean",
			payload: map[string]any{"input_constraints": map[string]any{
				"value_type": "boolean",
			}},
			input: "yes", expected: true,
		},
		{
			name: "boolean choice two rejects",
			payload: map[string]any{"input_constraints": map[string]any{
				"value_type": "boolean",
			}},
			input: "2", expected: false,
		},
		{
			name: "choice number",
			payload: map[string]any{"input_constraints": map[string]any{
				"value_type": "choice_id", "choices": []any{"alpha", "beta"},
			}},
			input: "2", expected: "beta",
		},
		{
			name: "choice id",
			payload: map[string]any{"input_constraints": map[string]any{
				"value_type": "choice_id", "choices": []any{"alpha", "beta"},
			}},
			input: "alpha", expected: "alpha",
		},
		{
			name: "text",
			payload: map[string]any{"input_constraints": map[string]any{
				"value_type": "string", "allow_empty": false,
			}},
			input: "hello", expected: "hello",
		},
		{
			name: "empty rejected",
			payload: map[string]any{"input_constraints": map[string]any{
				"value_type": "string", "allow_empty": false,
			}},
			wantErr: true,
		},
		{
			name: "cancel",
			payload: map[string]any{"input_constraints": map[string]any{
				"value_type": "choice_id", "choices": []any{"alpha"},
			}},
			input: "cancel", cancelled: true,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			value, cancelled, err := parseInteractionInput(test.payload, test.input)
			if (err != nil) != test.wantErr {
				t.Fatalf("unexpected error: %v", err)
			}
			if value != test.expected || cancelled != test.cancelled {
				t.Fatalf("got (%v, %v), want (%v, %v)", value, cancelled, test.expected, test.cancelled)
			}
		})
	}
}

func TestControlOutcomeConfirmsOrPreservesUncertainSteering(t *testing.T) {
	r := &Runner{
		unconfirmedControls: map[string]string{
			"accepted": "accepted direction",
			"rejected": "rejected direction",
		},
	}
	interrupts := make(chan os.Signal)

	for controlID, outcome := range map[string]string{
		"accepted": "admitted",
		"rejected": "already_done",
	} {
		err := r.handleChatEvent(
			context.Background(),
			"peer",
			"chat",
			protocol.ChatEvent{
				Type: "control_outcome",
				Payload: map[string]any{
					"control_id": controlID,
					"outcome":    outcome,
				},
			},
			interrupts,
		)
		if err != nil {
			t.Fatal(err)
		}
	}

	if len(r.unconfirmedControls) != 0 {
		t.Fatalf("controls remained unconfirmed: %#v", r.unconfirmedControls)
	}
	if len(r.pendingLines) != 1 || r.pendingLines[0] != "rejected direction" {
		t.Fatalf("unexpected preserved input: %#v", r.pendingLines)
	}
}

func TestUnconfirmedSteeringIsPreservedWhenChatEndsWithoutOutcome(t *testing.T) {
	r := &Runner{
		unconfirmedControls: map[string]string{
			"control-1": "first direction",
			"control-2": "second direction",
		},
		unconfirmedOrder: []string{"control-1", "control-2"},
	}

	r.preserveUnconfirmedControls()

	if len(r.unconfirmedControls) != 0 {
		t.Fatalf("controls remained unconfirmed: %#v", r.unconfirmedControls)
	}
	if len(r.pendingLines) != 2 ||
		r.pendingLines[0] != "first direction" ||
		r.pendingLines[1] != "second direction" {
		t.Fatalf("unexpected preserved input: %#v", r.pendingLines)
	}
}
