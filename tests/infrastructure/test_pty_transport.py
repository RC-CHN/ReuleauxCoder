import threading

import pytest

from reuleauxcoder.infrastructure.process.pty import _WinPtyTransport


def test_conpty_backpressure_serializes_data_without_owning_control_state():
    entered, release = threading.Event(), threading.Event()
    controls = []
    writes = []

    class Process:
        def write(self, text):
            writes.append(text)
            entered.set()
            assert release.wait(2)
            return len(text)

        def sendcontrol(self, key):
            controls.append(key)

        def setwinsize(self, rows, columns):
            controls.append((rows, columns))

        def close(self, *, force):
            controls.append("close")
            release.set()

    transport = _WinPtyTransport(Process())
    worker = threading.Thread(target=lambda: transport.write(b"first"), daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(TimeoutError, match="no input was sent"):
            transport.write(b"second")
        transport.interrupt()
        transport.resize(24, 80)
        transport.close()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert controls == ["c", (24, 80), "close"]
        assert writes == ["first"]
    finally:
        release.set()
        worker.join(timeout=1)
