//go:build linux || darwin

package terminal

import (
	"os"
	"syscall"
	"unsafe"
)

type windowSize struct {
	rows    uint16
	columns uint16
	xpixels uint16
	ypixels uint16
}

// Width returns the live terminal width without adding a peer dependency.
func Width(file *os.File) int {
	if file == nil {
		return 0
	}
	size := windowSize{}
	_, _, errno := syscall.Syscall(
		syscall.SYS_IOCTL,
		file.Fd(),
		uintptr(syscall.TIOCGWINSZ),
		uintptr(unsafe.Pointer(&size)),
	)
	if errno != 0 || size.columns == 0 {
		return 0
	}
	return int(size.columns)
}
