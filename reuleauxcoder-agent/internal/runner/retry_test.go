package runner

import (
	"errors"
	"testing"
	"time"
)

func TestDetectTerminalCapabilities(t *testing.T) {
	t.Setenv("COLUMNS", "132")
	t.Setenv("TERM", "xterm-256color")
	t.Setenv("COLORTERM", "truecolor")
	t.Setenv("NO_COLOR", "")
	t.Setenv("LANG", "en_US.UTF-8")

	terminal := detectTerminalCapabilities(true)

	if terminal.Width != 132 || terminal.ColorLevel != "truecolor" || !terminal.Unicode || !terminal.Interactive {
		t.Fatalf("unexpected terminal capabilities: %#v", terminal)
	}
}

func TestJitteredBackoffStaysWithinBound(t *testing.T) {
	base := 2 * time.Second
	for range 100 {
		delay := jitteredBackoff(base)
		if delay < base || delay > base+base/2 {
			t.Fatalf("delay %s outside [%s, %s]", delay, base, base+base/2)
		}
	}
}

func TestAuthenticationErrorClassification(t *testing.T) {
	if !isAuthenticationError(errors.New("http 401: invalid_peer_token")) {
		t.Fatal("401 should be classified as authentication error")
	}
	if isAuthenticationError(errors.New("connection reset")) {
		t.Fatal("network errors should remain retryable")
	}
}
