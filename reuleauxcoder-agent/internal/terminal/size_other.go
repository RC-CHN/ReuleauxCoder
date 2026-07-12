//go:build !linux && !darwin && !windows

package terminal

import "os"

// Width returns zero when no stdlib-only platform adapter is available.
func Width(file *os.File) int {
	return 0
}
