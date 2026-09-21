# TUI navigation and responsiveness

## Input rules

| Input | Behavior |
| --- | --- |
| Mouse wheel | Scroll the current panel, or the transcript when no panel is open, three rows per report. Preserve draft, history position and selection. |
| Up / Down in a draft | Move by displayed line, including wrapped Unicode text. Remember the preferred column through shorter lines and stop at the boundary. |
| Up in an empty draft | Recall input history. Down through history returns to the saved draft, cursor and image attachments. |
| Alt+Up / Alt+Down | Explicitly browse history with a nonempty draft. |
| Up / Down in a menu | Move selection and stop at the first/last entry. |
| PgUp / PgDn | Scroll the active content viewport with one row of overlap. |
| Home / End in an input | Move to the beginning/end of the logical input line. Ctrl+A / Ctrl+E move across the entire input. |
| Ctrl+Home / Ctrl+End | Go to the active content's beginning/end. Ctrl+End in the transcript resumes following output. |
| Shift+Tab in a form | Return to the previous field; Up/Down remain input editing keys. |

Wheel/page scrolling a menu does not select another item. If the selected item
has scrolled out of view, Enter first reveals it; a second Enter activates it.
Scrolling back in the transcript pauses following. New output shows an indicator;
resize and detail changes preserve a message-based reading anchor where possible.
Scroll down to the end or use Ctrl+End to follow again.

The alternate-screen TUI uses SGR mouse reports and disables terminal wheel-to-arrow
translation. It restores reporting modes on exit. Use `rcoder-tui --no-mouse` for
native terminal selection; use PgUp/PgDn to scroll the application in this mode.
`--no-alt-screen` does not enable mouse reporting. F1 opens the in-app key guide.

## Implementation and measurement

Navigation applies the full requested movement immediately, without the former
120 ms scroll easing or the normal 16 ms event batching delay. Background updates
remain batched. Editor rendering and cursor movement share cached Unicode display
geometry. Fitted ANSI rows use an LRU cache bounded to 1,024 entries and 1,048,576
UTF-16 code units across keys and values, excluding map overhead. Style closure and
wide-character clipping are identical on cache hits and misses.

Run from `reuleauxcoder-tui/`, with dependencies installed, and serialize benchmark
runs separately from tests and builds:

```bash
npm run benchmark -- --label local --json /tmp/tui-benchmark.json
npm run benchmark:navigation -- --label local --json /tmp/tui-navigation.json
```

The general benchmark measures layout work, idle/background CPU and controller
input to the next visible output write. That last measurement is a proxy: an
animation can satisfy it before the requested content is visible, particularly
in busy scenes. It also measures the first frame of the old easing animation,
not completion of the old movement.

The navigation benchmark injects real raw stdin reports through Ink and waits for
newly exposed content, with 40 samples per input type and a 3.5-second warmup.
It includes 10,000 transcript messages, a 5,000-line document, and an additional
1,000-sample cached cursor/layout test on a 500-line Unicode draft. Both benchmarks
use a synthetic 160×40 terminal sink, Node 24.16.0 on Linux, and a 120 FPS render
cap for the recorded runs. They exclude terminal emulator, GPU and SSH latency.
The original runs below used React's development mode. For measurements closer
to the shipped bundle, prefix both commands with `NODE_ENV=production`; newer
reports record `node_env` explicitly. Compare runs in the same mode.
CPU percentages are relative to one core and include the sampling intervals.
These are local observations, not a universal latency guarantee.

## Recorded results, 2026-09-21

Machine: Intel Xeon CPU Max 9470C. General benchmark comparison starts before
the navigation changes (`8eaad4e`) and ends with the bounded row cache (`ec4cccc`).

| General scene | Before input P95 (ms) | After input P95 (ms) | Before CPU (%) | After CPU (%) |
| --- | ---: | ---: | ---: | ---: |
| Panel page every 160 ms | 40.73 | 71.74 | 88.67 | 25.48 |
| Continuous panel pages | 46.78 | 36.10 | 99.89 | 88.76 |
| Transcript pages | 39.07 | 29.30 | 96.82 | 74.34 |
| Busy transcript pages | 33.32 | 39.90 | 99.34 | 93.83 |

The final run improved continuous scrolling and reduced CPU, but regressed
the first-write P95 for low-frequency panel paging and busy transcript paging.
The panel scene has only about 15 inputs, making its P95 particularly sensitive
to the slowest sample. The old easing also produced more, smaller frames; these
results do not establish that every navigation path got faster. Idle CPU was
0.16% before and 0.08% after, with no idle renders in either run.

The raw-input comparison isolates the row cache: both versions already have
immediate navigation, and the final column is a separate serialized repeat.

| Raw input → changed content | Before row cache P95 (ms) | First cache run P95 (ms) | Final repeat P95 (ms) |
| --- | ---: | ---: | ---: |
| Wheel | 54.19 | 47.17 | 50.51 |
| Document arrow | 39.69 | 33.05 | 34.53 |
| Typing | 27.96 | 24.71 | 24.15 |

Final medians were 29.12 / 26.91 / 23.41 ms respectively. The cached 500-line
Unicode cursor/layout microbenchmark had P95 0.01 ms; this excludes Ink rendering
and is not an end-to-end typing latency. The first cache run overlapped a short
test/build job during warmup; the final repeat ran after the general benchmark
and before tests. No claim is based on selecting the best run.

All recorded JSONs include additional render, CPU and output measurements:

- [General before](../reuleauxcoder-tui/benchmarks/navigation-before-2026-09-21.json)
- [General after](../reuleauxcoder-tui/benchmarks/navigation-after-2026-09-21.json)
- [Raw before cache](../reuleauxcoder-tui/benchmarks/navigation-raw-before-cache-2026-09-21.json)
- [Raw first cache run](../reuleauxcoder-tui/benchmarks/navigation-raw-cache-first-2026-09-21.json)
- [Raw final repeat](../reuleauxcoder-tui/benchmarks/navigation-raw-after-2026-09-21.json)

## Rendering follow-up

A Node CPU profile of the raw-input benchmark showed ANSI tokenization, style
diffing/serialization, Unicode width checks and garbage collection as the main
active costs. Ink rebuilds its output grid even for an animation-only commit;
memoizing React rows does not eliminate that work. The shipped bundle already
uses React production mode; switching the benchmark to production is a measurement
correction, not a new application optimization.

The three candidates are now implemented without changing animation timing:
bounded cross-frame ANSI/width caches, equal-style serialization shortcuts and
unchanged composed-row reuse. See [renderer measurements](tui-rendering-performance.md)
for the production-mode comparison and [adapter notes](../reuleauxcoder-tui/renderer/README.md)
for version guards, cache bounds and upstream byte-equivalence tests.

The raw benchmark now also verifies newly exposed wheel content and newly typed
text while the busy indicators animate. An unrelated spinner frame cannot satisfy
an input sample. Use production mode on both sides and serialize benchmarks
separately from test/build jobs when evaluating these candidates.
