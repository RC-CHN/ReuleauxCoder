---
name: pptx
metadata:
  rcoder.display-name: "Presentations"
  rcoder.display-name.zh-CN: "演示文稿"
  rcoder.summary: "Create, edit and inspect slide decks"
  rcoder.summary.zh-CN: "创建、修改和检查幻灯片"
  rcoder.icon: "presentation"
  rcoder.category: "documents"
description: Create, edit, inspect and preview PowerPoint presentations with deliberate visual design and slide-level validation.
---

# PowerPoint presentations

Determine the audience, purpose, source material, editable output and any existing template. Honor supplied content and branding; use sensible defaults for unspecified details. Do not invent evidence, metrics or quotes to fill a slide.

Work on the host that owns the input and execution tools. Resolve this skill's resources from its actual catalog location. Use an isolated Python environment with `python-pptx` for creation/editing; [scripts/inspect_deck.py](scripts/inspect_deck.py) uses the same package. Rendering needs a local LibreOffice installation and optionally PyMuPDF. Detect these first and prepare only dependencies needed for the requested operation, within existing authorization.

## Choose the path

- **Read or edit:** inspect the original with `python <skill-dir>/scripts/inspect_deck.py input.pptx`. Read [references/editing.md](references/editing.md) before modifying an existing deck.
- **Create:** read [references/design.md](references/design.md) and [references/creation.md](references/creation.md). Build a short content outline, then choose layouts that fit each claim.
- **Preview or convert:** follow [references/preview.md](references/preview.md). All instructions are contained in this directory; there is no required image-generation or proprietary reviewer service.

Preserve the supplied deck by default and write a distinct deliverable. In-place changes require that user intent and a recoverable copy. Do not flatten an editable deck into screenshots unless requested.

## Finish with evidence

Run the inspector, fix out-of-bounds objects and examine warnings. It does not determine text fit, visual quality or PowerPoint compatibility. Render and actually view the slides at readable size; inspect long titles, dense charts, font substitutions, overlap and cropped content. Revise and rerender affected slides.

If rendering is unavailable, report structural checks separately and explicitly state that visual validation is incomplete. Deliver the PPTX path, requested previews and a concise account of changes and unresolved limitations.
