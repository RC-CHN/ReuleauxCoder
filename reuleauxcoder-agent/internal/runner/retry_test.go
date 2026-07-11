package runner

import (
	"errors"
	"testing"
	"time"
)

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
