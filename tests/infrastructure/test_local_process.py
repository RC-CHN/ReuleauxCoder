import shlex
import sys
import threading
import time

from reuleauxcoder.domain.process import ProcessCursor, ProcessState
from reuleauxcoder.infrastructure.process.local import LocalProcessPort
from reuleauxcoder.infrastructure.platform import ShellType, get_platform_info


def test_timeout_preserves_and_streams_partial_output(tmp_path) -> None:
    chunks = []
    command = f"{shlex.quote(sys.executable)} -u -c " + shlex.quote(
        "import time\n"
        "print('line-0', flush=True)\n"
        "print('line-1', flush=True)\n"
        "time.sleep(30)\n"
    )

    result = LocalProcessPort().run(
        command,
        cwd=str(tmp_path),
        timeout=1,
        stream_handler=chunks.append,
    )

    assert result.timed_out is True
    assert "line-0" in result.stdout
    assert "line-1" in result.stdout
    assert "".join(chunk.data for chunk in chunks) == result.stdout


def test_cancellation_returns_promptly_without_reaping(tmp_path) -> None:
    cancellation = threading.Event()
    shell = get_platform_info().get_preferred_shell()
    if shell is ShellType.BASH:
        sleeper = "sleep 30"
    elif shell in (ShellType.POWERSHELL, ShellType.POWERSHELL_CORE):
        sleeper = "Start-Sleep -Seconds 30"
    else:
        sleeper = "timeout /t 30 /nobreak"

    def cancel_soon() -> None:
        time.sleep(0.2)
        cancellation.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    result = LocalProcessPort().run(
        sleeper,
        cwd=str(tmp_path),
        timeout=60,
        cancellation_event=cancellation,
    )
    elapsed = time.monotonic() - started

    assert result.cancelled is True
    # Termination signals fire and the caller unwinds without waiting for the
    # process group to actually die (reaped asynchronously).
    assert elapsed < 1.5


def test_invalid_utf8_is_replaced_and_reported_as_a_fact(tmp_path) -> None:
    port = LocalProcessPort()
    command = f"{shlex.quote(sys.executable)} -u -c " + shlex.quote(
        "import sys; sys.stdout.buffer.write(b'valid\\xffbytes')"
    )
    handle = port.start(
        command,
        cwd=str(tmp_path),
        runtime_timeout=5,
    )
    cursor = ProcessCursor()
    output = []
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = port.poll(handle.session_id, cursor=cursor, wait_ms=100)
        cursor = snapshot.cursor
        output.append(snapshot.stdout)
        if snapshot.state is ProcessState.EXITED:
            break
    else:
        raise AssertionError("process did not exit")

    assert "".join(output) == "valid\ufffdbytes"
    assert snapshot.output_decode_replaced is True
    port.release(handle.session_id)
