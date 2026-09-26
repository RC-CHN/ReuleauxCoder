# Edit with fidelity

Inspect paragraphs, runs, styles, tables, headers/footers, comments, hyperlinks and tracked changes before editing. The bundled inspector reports raw revision presence; its text inventory is not a resolved accept/reject view.

Modify targeted runs when formatting matters. Assigning `paragraph.text` replaces its run structure and may lose formatting or links. Preserve document sections, numbering, relationships, media and untouched package parts. A `.docx` is an OOXML ZIP package; a `.doc` must first be converted by a compatible application.

For comments and tracked changes, inspect the installed library's actual support. Do not simulate tracked changes using red text. OOXML revisions require valid author/date/IDs, inserted/deleted run structures and relationships; comments require matching anchors and comment parts. If using low-level XML, preserve namespaces and validate in a compatible editor.

Do not accept/reject existing revisions unless requested. A library that cannot preserve a feature is not a safe round-trip choice for that document. Use a compatible editor or a targeted package-part edit with explicit verification instead.

Save to a distinct destination, reopen, compare requested text and counts, and render representative pages. Verify that comments or revisions remain reviewable in the intended application when they are part of the deliverable.
