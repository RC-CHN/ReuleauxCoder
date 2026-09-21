# TUI rendering optimization, 2026-09-21

Animation remains at 60 FPS with the existing independent 120 FPS render cap.
No animation suppression, reduced frequency or input debouncing is involved.
The change reduces the work Ink performs to construct each terminal frame.

## Changes

1. `dfb5091`: retain bounded ANSI parsing, character-width and block-width caches
   across frames, scoped to each terminal root through a WeakMap.
2. `96c2f08`: skip style diff construction for adjacent characters with identical
   reduced ANSI styles, retaining upstream transitions and final closing codes.
3. `20ebc36`: reuse composed row strings when width and ordered clipped/transformed
   writes match. Unchanged rows bypass grid construction, character overlays,
   width lookups and serialization. Transformers still execute before lookup.

The [adapter](../reuleauxcoder-tui/renderer/README.md) checks the pinned Ink version
and original source hashes. Install/build/bundle hooks apply the same changes to
development runs and production bundles. Updating Ink requires reviewing this
boundary; this is an explicit maintenance cost of the optimization.

## Method

Baseline commit: `5505050`, unmodified Ink renderer. Both before and after use
React production mode, Node 24.16.0, Linux, Intel Xeon CPU Max 9470C and a 160×40
synthetic terminal. Benchmarks were run serially, separately from builds/tests.
The navigation benchmark waits for newly exposed text after raw stdin input;
unrelated animation writes do not satisfy a sample. Each input scene has 40
samples after warmup, with 10,000 underlying messages and a 5,000-line document.

```bash
cd reuleauxcoder-tui
NODE_ENV=production npm run benchmark -- --label local --json /tmp/render.json
NODE_ENV=production npm run benchmark:navigation -- --label local --json /tmp/input.json
```

The general benchmark's input metric means the next visible write, which can be
an animation write; use the raw content metric when evaluating input latency.
CPU is relative to one core and includes the configured input pacing. Values
above 100% can include Node's additional runtime threads. These runs exclude
terminal emulator/GPU/SSH costs and are not a universal latency guarantee.

## Results

Raw stdin to newly visible content, P95 milliseconds. Each stage includes the
preceding changes; the final column is a separate repeat of the complete version.

| Input | Baseline | Cross-frame cache | Equal styles | Row reuse | Final repeat |
| --- | ---: | ---: | ---: | ---: | ---: |
| Wheel | 46.26 | 38.08 | 36.04 | 21.62 | 22.25 |
| Document arrows | 35.08 | 25.92 | 22.72 | 12.48 | 12.72 |
| Typing | 24.45 | 25.51 | 20.37 | 13.01 | 13.70 |
| Wheel while busy | 28.62 | 29.57 | 29.43 | 29.06 | 28.47 |
| Typing while busy | 32.78 | 27.55 | 28.52 | 26.11 | 26.94 |

The final repeat improved ordinary wheel/arrow/typing P95 by about 52% / 64% /
44%, respectively. Busy typing improved by about 18%; busy wheel P95 was
essentially unchanged. Not every intermediate stage improves every metric.
Final ordinary-input medians were 11.80 / 10.43 / 9.99 ms. The cached Unicode
editor microbenchmark remained at P95 0.01 ms and is not an end-to-end metric.

General benchmark, before versus the complete version:

| Scene | Ink render P95 before (ms) | After (ms) | CPU before (%) | After (%) |
| --- | ---: | ---: | ---: | ---: |
| Idle RPC | 0 | 0 | 0.08 | 0.07 |
| Busy animation | 18.79 | 8.46 | 98.17 | 98.35 |
| Panel pages every 160 ms | 23.23 | 12.99 | 22.01 | 19.94 |
| Continuous panel pages | 32.39 | 15.27 | 86.59 | 69.56 |
| Transcript pages | 19.36 | 9.93 | 62.36 | 50.04 |
| Busy transcript pages | 18.68 | 11.12 | 93.13 | 87.53 |
| Background output | 19.66 | 9.92 | 61.34 | 44.15 |

Pure busy-animation total CPU did **not** improve, despite lower measured Ink
render time. Busy transcript next-write P95 was also slightly worse, 36.94 to
37.71 ms; it is a proxy and not evidence that content-level latency improved.
Background output handled more renders (65 to 93 in approximately 2.5 seconds)
with lower CPU. The benefit is workload-dependent, not a blanket CPU reduction.

Raw artifacts:

- [General baseline](../reuleauxcoder-tui/benchmarks/renderer-before-2026-09-21.json)
- [General complete version](../reuleauxcoder-tui/benchmarks/renderer-after-2026-09-21.json)
- [Input baseline](../reuleauxcoder-tui/benchmarks/renderer-input-before-2026-09-21.json)
- [Input cross-frame cache](../reuleauxcoder-tui/benchmarks/renderer-input-cache-2026-09-21.json)
- [Input equal styles](../reuleauxcoder-tui/benchmarks/renderer-input-style-2026-09-21.json)
- [Input row reuse](../reuleauxcoder-tui/benchmarks/renderer-input-rows-2026-09-21.json)
- [Input final repeat](../reuleauxcoder-tui/benchmarks/renderer-input-final-2026-09-21.json)

## Correctness and limits

- 1,000 seeded output combinations compare byte for byte against the saved,
  hash-verified upstream renderer, including Unicode, clipping and overlays.
- ANSI transition comparisons include bold/dim resets, truecolor, palette colors,
  inverse text and OSC hyperlinks; style array identity is not assumed.
- Row invalidation tests cover width, placement, paint order and changing
  transformer closure state. Cache capacity, oversized entries and terminal
  isolation have explicit checks.
- The complete 73-test TUI suite passed, including cursor behavior, resizing,
  native selection mode and real PTY runs of source and standalone bundle.
- A fresh `npm ci` in a separate temporary project applied the adapter automatically
  and passed all five renderer tests; validation does not depend on manual edits
  to the original working directory's dependencies. Ruff also passed.

Cache limits bound entries and retained payload accounting units, not exact V8
heap bytes. Cold/missed rows still need composition; Ink's layout traversal and
final terminal diff also remain. A scrolling frame and an animation frame have
different reuse opportunities, so improvements need not be uniform.
