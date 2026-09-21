# Tool I/O and performance

Tool-output projections count line boundaries without materializing all lines,
locate only retained head/tail spans, and copy only characters within the output
budget. Existing splitlines and retention semantics are preserved. Full-output
archives encode 64 Ki-character chunks and hash the exact bytes as they are
written, avoiding repeated full-output UTF-8 allocations.

Shell waits pass the remaining duration to the process adapter in one poll.
Local condition waits and host-side remote future waits check cancellation every
50 ms without issuing extra peer RPCs. Cancelling a remote wait only detaches the
request; it does not terminate the process or advance the output cursor. A later
poll retrieves output using the same cursor, including output from a late reply.
Peers advertising `process.poll.concurrent` execute polls separately from command
dispatch, with at most 64 pending polls and cleanup on disconnect. Older peers
retain 50 ms poll requests so a waiting process cannot block control commands.

`read_file` applies `offset` and `limit` in the workspace adapter. Local reads
scan bounded chunks and stop after the requested page plus lookahead. Remote
reads use the `workspace.fs.read_text_page` capability and return only that page.
Older peers must be upgraded for paged reads; `override=true` explicitly retains
the full-file path.

Pages retain at most 256 Ki characters, including normalized line separators.
An oversized line produces an explicit partial-page notice. Total line count is
reported only when the reader reaches EOF; otherwise `has_more` and a continuation
offset are provided. CRLF and the other Python `splitlines` separators preserve
line numbering. Seeking to a large line offset still scans earlier content,
without retaining it or transferring it from the peer.

`list_file` reports incomplete scans even when the scanned entries do not match
the filter. Its scan budget must never be interpreted as proof of no matches.

Glob traversal checks whether a directory can contain a future match before
descending. Patterns such as `src/*.py` skip dependency trees and nested source
directories, while `src/**/*.py` retains recursive matching. Matching paths remain
relative to the requested root and results remain sorted by modification time.
