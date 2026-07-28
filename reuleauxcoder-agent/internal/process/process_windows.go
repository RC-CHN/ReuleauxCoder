//go:build windows

package process

import (
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"unsafe"
)

const (
	jobObjectExtendedLimitInfoClass = 9
	jobObjectLimitKillOnJobClose    = 0x00002000
	processSetQuota                 = 0x0100
	processTerminate                = 0x0001
)

var (
	kernel32                 = syscall.NewLazyDLL("kernel32.dll")
	createJobObjectW         = kernel32.NewProc("CreateJobObjectW")
	setInformationJobObject  = kernel32.NewProc("SetInformationJobObject")
	openProcess              = kernel32.NewProc("OpenProcess")
	assignProcessToJobObject = kernel32.NewProc("AssignProcessToJobObject")
	terminateJobObject       = kernel32.NewProc("TerminateJobObject")
	closeHandle              = kernel32.NewProc("CloseHandle")
)

type jobObjectBasicLimitInformation struct {
	perProcessUserTimeLimit int64
	perJobUserTimeLimit     int64
	limitFlags              uint32
	minimumWorkingSetSize   uintptr
	maximumWorkingSetSize   uintptr
	activeProcessLimit      uint32
	affinity                uintptr
	priorityClass           uint32
	schedulingClass         uint32
}

type ioCounters struct {
	readOperationCount  uint64
	writeOperationCount uint64
	otherOperationCount uint64
	readTransferCount   uint64
	writeTransferCount  uint64
	otherTransferCount  uint64
}

type jobObjectExtendedLimitInformation struct {
	basicLimitInformation jobObjectBasicLimitInformation
	ioInfo                ioCounters
	processMemoryLimit    uintptr
	jobMemoryLimit        uintptr
	peakProcessMemoryUsed uintptr
	peakJobMemoryUsed     uintptr
}

type processTreeHandle struct {
	mu   sync.Mutex
	job  syscall.Handle
	once sync.Once
}

func configureProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}

func attachProcessTree(cmd *exec.Cmd) (*processTreeHandle, error) {
	if cmd.Process == nil {
		return nil, fmt.Errorf("process has no Windows handle")
	}
	jobValue, _, callErr := createJobObjectW.Call(0, 0)
	if jobValue == 0 {
		return nil, windowsCallError("CreateJobObjectW", callErr)
	}
	handle := &processTreeHandle{job: syscall.Handle(jobValue)}
	limits := jobObjectExtendedLimitInformation{}
	limits.basicLimitInformation.limitFlags = jobObjectLimitKillOnJobClose
	set, _, callErr := setInformationJobObject.Call(
		jobValue,
		jobObjectExtendedLimitInfoClass,
		uintptr(unsafe.Pointer(&limits)),
		unsafe.Sizeof(limits),
	)
	if set == 0 {
		handle.close()
		return nil, windowsCallError("SetInformationJobObject", callErr)
	}
	processHandle, _, callErr := openProcess.Call(
		processSetQuota|processTerminate,
		0,
		uintptr(cmd.Process.Pid),
	)
	if processHandle == 0 {
		handle.close()
		return nil, windowsCallError("OpenProcess", callErr)
	}
	defer closeHandle.Call(processHandle)
	assigned, _, callErr := assignProcessToJobObject.Call(jobValue, processHandle)
	if assigned == 0 {
		handle.close()
		return nil, windowsCallError("AssignProcessToJobObject", callErr)
	}
	return handle, nil
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

func terminateProcessTree(cmd *exec.Cmd, processTree *processTreeHandle) error {
	if processTree != nil && processTree.terminate() == nil {
		return nil
	}
	if cmd.Process == nil {
		return nil
	}
	kill := exec.Command("taskkill", "/PID", strconv.Itoa(cmd.Process.Pid), "/T", "/F")
	output, err := kill.CombinedOutput()
	if err != nil {
		return fmt.Errorf("taskkill failed: %w: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func reapProcessTreeAfterRootExit(
	cmd *exec.Cmd,
	processTree *processTreeHandle,
) {
	_ = cmd
	if processTree != nil {
		processTree.close()
	}
}

func (handle *processTreeHandle) terminate() error {
	handle.mu.Lock()
	defer handle.mu.Unlock()
	if handle.job == 0 {
		return nil
	}
	result, _, callErr := terminateJobObject.Call(uintptr(handle.job), 1)
	if result == 0 {
		return windowsCallError("TerminateJobObject", callErr)
	}
	return nil
}

func (handle *processTreeHandle) close() {
	handle.once.Do(func() {
		handle.mu.Lock()
		job := handle.job
		handle.job = 0
		handle.mu.Unlock()
		if job != 0 {
			closeHandle.Call(uintptr(job))
		}
	})
}

func windowsCallError(operation string, callErr error) error {
	if callErr != nil && callErr != syscall.Errno(0) {
		return fmt.Errorf("%s failed: %w", operation, callErr)
	}
	return fmt.Errorf("%s failed", operation)
}
