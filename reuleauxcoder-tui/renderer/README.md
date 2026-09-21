# Ink rendering adapter

This directory reduces repeated work in Ink's terminal output composition. It
does not change animation frequency, keyboard routing or terminal diff output.

`scripts/patch-renderer.mjs` adapts the pinned Ink 7.1.1 `output.js` and
`renderer.js`. It verifies the package version and SHA-256 of the original files
before writing either file. Original files remain beside the generated versions
as `*.rcoder-original.js` for differential tests. Installation is idempotent;
unknown upstream versions or contents fail explicitly.

The adapter runs after `npm ci`/`npm install`, before development/build/benchmark
commands, and inside the bundle script. The production bundle includes the same
helpers; the installed Python wheel needs no patching or npm dependencies.
After updating Ink, review the upstream changes and renew the hashes only after
the differential tests and real terminal tests pass. Do not edit node_modules
manually to update this adapter.

Cross-frame caches belong to an Ink root via a WeakMap, so separate terminal
instances cannot retain each other's cells. Cache keys include the entire ANSI
text, preserving colors, hyperlinks and other style changes. Parsed cells are
treated as immutable; overlay operations replace grid cells rather than modify
the parsed records. Screen-reader output keeps Ink's original path.

Cache accounting limits are deliberately distinct from measured heap bytes:

| Cache | Entries | Payload accounting limit |
| --- | ---: | ---: |
| String widths | 4,096 | 65,536 key code units plus value slots |
| Block widths | 128 | 65,536 key code units plus value slots |
| Parsed ANSI rows | 512 | 2,097,152 units including text, cells and styles |
| Composed rows | 256 | 2,097,152 key/value UTF-16 code units |

Oversized values are computed without retention. Cache eviction affects work
performed, never output. Style serialization only skips a transition when the
two reduced style arrays have identical codes and closing codes; all other
transitions and final closures use the upstream ANSI library.

Composed-row keys include width and every clipped/transformed write's x position
and complete text in paint order. Transformers still run on every frame; their
results form the key, so changing closure state cannot return stale content.
Hits skip cell-grid allocation, overlays, width lookups and ANSI serialization.
Misses retain Ink's wide-character overlap behavior, including blank trailing
cells and trimming. Height/y affect which writes reach a row, not its cache key.

`test/renderer.test.ts` compares optimized output byte for byte with the
unmodified implementation, including seeded overlapping writes, Unicode,
clipping, transformed rows, resets and links. It also checks installation guards,
cache bounds and isolation. App-level cursor and PTY tests cover the integration.
