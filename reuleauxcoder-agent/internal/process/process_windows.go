//go:build windows

package process

import (
	"fmt"
	"os/exec"
	"strconv"
	"syscall"
)

func configureProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}

func interruptProcessTree(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	generateConsoleCtrlEvent := kernel32.NewProc("GenerateConsoleCtrlEvent")
	const ctrlBreakEvent = uintptr(1)
	result, _, callErr := generateConsoleCtrlEvent.Call(
		ctrlBreakEvent,
		uintptr(cmd.Process.Pid),
	)
	if result == 0 {
		return fmt.Errorf("GenerateConsoleCtrlEvent failed: %w", callErr)
	}
	return nil
}

func terminateProcessTree(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	kill := exec.Command("taskkill", "/PID", strconv.Itoa(cmd.Process.Pid), "/T", "/F")
	_ = kill.Run()
}

func reapProcessTreeAfterRootExit(cmd *exec.Cmd) {
	// taskkill cannot reliably discover descendants once the root has exited.
	// Avoid spawning taskkill after every normally completed command; live-root
	// termination still uses terminateProcessTree.
	_ = cmd
}
