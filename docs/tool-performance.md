# Tool I/O and performance

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
