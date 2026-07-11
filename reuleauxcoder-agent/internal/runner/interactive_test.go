package runner

import (
	"context"
	"os"
	"testing"
	"time"
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
