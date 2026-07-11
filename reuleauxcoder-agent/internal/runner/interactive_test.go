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
