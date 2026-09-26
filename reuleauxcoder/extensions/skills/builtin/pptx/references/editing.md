# Edit without losing the template

Inventory slides in presentation order, including titles, text runs, tables, charts, images, notes and grouped objects. Identify requested targets by both content and position; a ZIP part filename is not the slide order.

Preserve masters, themes, slide dimensions, notes and unrelated objects. For text-only corrections, edit the relevant runs rather than replacing a complete text frame and losing formatting. When replacing a chart, preserve units and source data; inspect the updated chart visually.

`python-pptx` is useful for ordinary text, table, image and chart operations, but does not expose every animation, transition or relationship edit. Do not promise preservation of unsupported features. Compare affected package parts and use the target application where fidelity matters. Avoid undocumented XML edits unless their relationships and ordering are understood and verified.

Write a new output, reopen it and compare slide count and requested content. Run the inspector, render changed slides and examine surrounding slides for layout drift. An intentional off-canvas decorative element may explain a geometry warning; do not delete it automatically.
