---
name: docs-translate
description: Translate or synchronize documentation while preserving facts, terminology, code, links and markup.
---

# Translate and synchronize documentation

Determine source/target languages, corresponding files and the glossary from the task and project conventions. Do not assume English always owns the facts. Use the diff for incremental synchronization; preserve still-valid material unique to the target.

Preserve negation, conditions, obligation, exceptions and failure consequences. Resolve technical ambiguity against implementation or authoritative definitions. Adjust sentence structure naturally while respecting the author's voice.

- Preserve code, flags, identifiers, configuration keys, placeholders and real paths. Translate comments only when they are reader-facing.
- Keep corresponding structure and check anchors affected by translated headings. Use existing localized link targets; do not invent pages.
- Preserve Markdown fences, table columns, template variables and MDX/site directives.
- Follow the project glossary, typography and established Chinese/English spacing.
- Do not silently change product behavior during translation. If the source is wrong, identify it and correct both sides only within the task's scope.

Check omitted paragraphs, residual source-language text, broken links and unintended code edits; build the docs when warranted. Report updated language pairs and unresolved terms or missing pages.
