# Workspace text search

`grep` accepts `pattern`, `path`, `include`, `literal` and `include_ignored`.
`literal=true` treats the pattern as plain text. Include globs are relative to the
search directory: `*.py` matches at any depth, `src/**/*.py` selects that subtree.
An explicit file bypasses directory/include filters but still respects limits.

In a Git working tree, when Git is installed, `git ls-files` selects tracked and
untracked non-ignored files. Git owns nested ignore rules, negation and repository
excludes. `include_ignored=true` uses directory traversal instead. Outside Git, or
when Git is unavailable, search also uses directory traversal. Dependency/cache
directories supplied by the host tool are pruned, including `.git`, `.venv`,
`node_modules` and `.rcoder`. Recursive search does not follow file symlinks or
symlink directories. NUL bytes identify binary files; other bytes are decoded as
UTF-8 with replacement. This binary check is incremental: matches before a later
NUL can already have been returned.

## Resource limits

`WorkspaceSearchLimits` supplies host defaults, which are sent to the peer. The
peer retains equivalent defaults for older hosts' literal search requests.

| Limit | Default |
| --- | --- |
| Candidate files | 5,000; at most 20,000 enumerated file entries |
| Matching lines | 200 |
| Individual file | 2 MiB |
| Total bytes read | 32 MiB |
| Returned line | 2,000 Unicode characters |
| Formatted matching records | 32,000 characters, including paths and truncation markers |
| Search deadline | 5 seconds |
| Single local regex match | 100 milliseconds |

Results preserve file/line references, scan counters and explicit partial reasons:
`entry_limit`, `file_limit`, `file_size`, `scan_bytes`, `line_chars`, `output_chars`,
`match_limit`, `timeout`, `regex_timeout`, or `cancelled`. A short partial-result
notice is added outside the matching-record budget. Matching precedes line
shortening. Narrow path/include or use `read_file` for the referenced file.
Reaching the match limit is conservatively reported as partial even if that was
the last matching line. A one-byte probe can exceed a byte budget to detect
exhaustion. Oversized files are skipped before reading.

Local regex uses the `regex` package's Python-compatible VERSION0 syntax and timed
matching. It is now a direct dependency, previously brought in by tiktoken. The
matcher releases the GIL. Cancellation/deadlines are checked between reads; they
do not forcibly interrupt an OS filesystem read. Git discovery has a subprocess
timeout. Timed matching adds overhead to short-line workloads; reduced scan scope
and bounded allocation are the main wins.

## Remote execution and compatibility

Peers advertising `workspace.fs.search_text.bounded` handle the entire query in
one `fs.search_text` request using Go's standard-library regexp engine, without
Python or ripgrep. Unsupported expressions return an error. Older peers must be
upgraded for the new host's grep tool; there is no per-file download fallback.

Local and remote syntax are documented separately. Go's RE2-style engine does not
support lookaround/backreferences; its `\w`, `\d`, `\s` and word boundaries use
ASCII semantics. Use explicit ranges for portable regexes, or `literal=true`.
Line numbers retain Python `splitlines` separators on both sides. Include globs
are case-sensitive and share segment/`**` semantics. Remote include globs support
`*`, `?` and `**`; character classes (`[...]`) return an explicit error because
Python and Go interpret those classes differently.

Remote cancellation is checked before dispatch. In-flight requests finish under
the peer's search deadline; this change adds no remote cancellation protocol.
Transport failures retain the existing relay timeout. The peer's deadline bounds
cooperative search work, not uninterruptible filesystem operations.

## Benchmark

Run `python scripts/benchmark-search.py --baseline-ref e62dbb5` for a generated
fixture, or add `--workspace . --pattern 'your regex'` for a real checkout. The
script executes the adapter from the supplied trusted Git revision, reports median
wall time over three searches, and separately measures peak Python allocations
using tracemalloc. Result counts and truncation accompany timing because ignore
rules and budgets can change coverage.

For the peer, run `go test ./internal/workspace -run '^$' -bench
BenchmarkSearchWorkspace -benchmem` from `reuleauxcoder-agent`. This searches 500
files in one native operation, excluding transport latency.

Recorded on Linux amd64, Python 3.12, Intel Xeon CPU Max 9470C, against `e62dbb5`:

| Workload | Before | After | Peak Python allocations before → after |
| --- | --- | --- | --- |
| Checkout query `WorkspaceSearchResult\|workspace\.fs\.search_text\|search_text_via_primitives` | 3,806 ms | 431 ms | 450.9 → 0.065 MiB |
| Generated fixture: 500 short-line files plus ignored 16 MiB map, no matches | 65 ms | 255 ms | 32.2 → 0.060 MiB |

The checkout query returned 19/20 matches respectively, with both searches partial:
the old scan exhausted its traversal budget; the new scan skipped oversized files.
These numbers measure the complete policy change, not an identical-scope regex
engine comparison. The short-line case shows the cost of per-match timeouts; it
is retained as a regression benchmark rather than presenting only favorable cases.
The native Go benchmark measured about 47 ms per 500-file query (42 MB/s,
6.7 MB allocated per operation). All figures are machine/workload-specific.
