//go:build !windows

package process

import (
	"os/exec"
	"syscall"
	"time"
)

type processTreeHandle struct{}

func configureProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func attachProcessTree(cmd *exec.Cmd) (*processTreeHandle, error) {
	_ = cmd
	return &processTreeHandle{}, nil
}

func interruptProcessTree(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	err := syscall.Kill(-cmd.Process.Pid, syscall.SIGINT)
	if err == syscall.ESRCH {
		return nil
	}
	return err
}

func terminateProcessTree(cmd *exec.Cmd, processTree *processTreeHandle) {
	_ = processTree
	if cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
	time.Sleep(100 * time.Millisecond)
	if syscall.Kill(-cmd.Process.Pid, 0) == nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
}

func reapProcessTreeAfterRootExit(
	cmd *exec.Cmd,
	processTree *processTreeHandle,
) {
	_ = processTree
	if cmd.Process == nil {
		return
	}
	processGroup := -cmd.Process.Pid
	if syscall.Kill(processGroup, 0) != nil {
		return
	}
	_ = syscall.Kill(processGroup, syscall.SIGTERM)
	time.Sleep(100 * time.Millisecond)
	if syscall.Kill(processGroup, 0) == nil {
		_ = syscall.Kill(processGroup, syscall.SIGKILL)
	}
}
