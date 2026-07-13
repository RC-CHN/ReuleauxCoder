import shlex
import sys

from reuleauxcoder.infrastructure.process.local import LocalProcessPort


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
