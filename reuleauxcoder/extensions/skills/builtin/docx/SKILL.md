---
name: docx
metadata:
  rcoder.display-name: "Word documents"
  rcoder.display-name.zh-CN: "Word 文档"
  rcoder.summary: "Create and edit documents with structure and formatting"
  rcoder.summary.zh-CN: "创建和修改具有结构与排版的文档"
  rcoder.icon: "document"
  rcoder.category: "documents"
description: Create, edit and inspect Word documents, preserving structure and validating layout, comments and revisions.
---

# Word documents

Establish the document's reader, purpose, required sections, template and output format. Follow existing styles when editing. Preserve original inputs and deliver a new file unless in-place editing was requested.

Use `python-docx` in an isolated task environment for ordinary creation/editing. The bundled [scripts/inspect_doc.py](scripts/inspect_doc.py) uses only Python's standard library to inventory text, styles, tables, revisions and comments. It does not render pages or accept/reject changes.

- For a new document, read [references/authoring.md](references/authoring.md).
- For editing, tracked changes or comments, read [references/editing.md](references/editing.md).
- For PDF output and visual checks, read [references/preview.md](references/preview.md).

Resolve these resources relative to this skill's actual location on the execution host. Detect dependencies and prepare only what the task needs; do not modify the rcoder installation environment just to add document libraries.

## Quality and delivery

Use real headings, lists and table structure, meaningful link text and readable contrast. Choose installed fonts with the required language coverage; do not replace unsupported characters with different wording. Avoid layout made entirely from blank lines or spaces.

Reopen the DOCX and compare requested content, tables and revision state. Run the inspector; for edits, compare before and after. Render and actually view pages to check pagination, blank pages, broken rows, headers/footers, clipping and font substitution. Structural checks alone do not validate page layout.

Deliver the DOCX and requested exports, explain meaningful changes, and state any unverified rendering or unsupported feature preservation.
