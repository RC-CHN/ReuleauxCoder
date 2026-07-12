//go:build windows

package terminal

import (
	"os"
	"syscall"
	"unsafe"
)

type coord struct {
	x int16
	y int16
}

type smallRect struct {
	left   int16
	top    int16
	right  int16
	bottom int16
}

type consoleScreenBufferInfo struct {
	size              coord
	cursorPosition    coord
	attributes        uint16
	window            smallRect
	maximumWindowSize coord
}

var getConsoleScreenBufferInfo = syscall.NewLazyDLL("kernel32.dll").NewProc(
	"GetConsoleScreenBufferInfo",
)

// Width returns the live Windows console width without adding a dependency.
func Width(file *os.File) int {
	if file == nil {
		return 0
	}
	info := consoleScreenBufferInfo{}
	ok, _, _ := getConsoleScreenBufferInfo.Call(
		file.Fd(), uintptr(unsafe.Pointer(&info)),
	)
	if ok == 0 || info.window.right < info.window.left {
		return 0
	}
	return int(info.window.right-info.window.left) + 1
}
