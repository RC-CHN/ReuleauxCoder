# Builtin skills

The core ships 17 skills for CLI, TUI and VS Code. `/skills` lists them and `/skills reload` refreshes skill files. Descriptions enter the catalog; instruction bodies and task references are read only when needed. Skills use English instructions and respond in the user's language.

| Skill | Use |
| --- | --- |
| `rcoder-config` | Edit and check rcoder configuration, including reasoning and global/workspace inheritance |
| `skill-creator` | Create, improve or capture a repeatable workflow as a skill |
| `skill-installer` | Install complete local/Git skill directories without overwriting existing edits |
| `project-guide` | Maintain project instructions from verified commands and architecture |
| `code-review` | Review correctness, concurrency, security and compatibility |
| `code-simplify` | Reduce duplicated state, unnecessary abstractions and coupling |
| `test-and-fix` | Diagnose test failures and verify fixes |
| `github-ci` | Inspect and follow PR checks, diagnose CI and maintain PR descriptions |
| `ui-acceptance` | Verify actual user journeys and visible feedback |
| `docs-writing` | Write technical documentation from actual product behavior |
| `docs-translate` | Translate and synchronize documentation without changing meaning/code |
| `text-polish` | Edit prose while preserving facts and the author's voice |
| `structured-data` | Query and transform JSON, JSONL and CSV |
| `pptx` | Create/edit presentations and inspect content and geometry |
| `docx` | Create/edit Word documents and inspect text, comments and revisions |
| `xlsx` | Create/analyze workbooks and check formulas, errors and result caches |
| `pdf` | Create/convert PDFs and inspect, extract, merge, select and render pages |

Each directory contains its own instructions, references and scripts. There are no dependencies on development reference checkouts, sibling skill resources or a proprietary tool runtime. The text-polish directory includes the MIT license for its reused upstream text. Other resources are adapted for rcoder's actual file-based workflows.

## Scope and customization

Workspace `.rcoder/skills/<name>/SKILL.md` overrides the same name in the core host's `~/.rcoder/skills/`, which overrides the builtin. Edit a custom copy rather than the installed package. `/skills disable <name>` disables the resolved name, including overrides; `/skills enable <name>` reverses that. Disabling project/user scanning does not disable builtins.

Skills guide existing file, shell, browser and other available tools; they do not add permissions or automatically execute scripts. Their scope and resource paths belong to the host doing the work. In VS Code Remote this is normally the workspace host; relay tools may run elsewhere.

## Document dependencies and checks

Office packages are optional task dependencies, not part of the core's runtime requirements. Prepare an isolated environment on the actual execution host when needed:

| Operation | Dependency |
| --- | --- |
| PPTX create/edit/inspect | `python-pptx` |
| DOCX create/edit | `python-docx`; the bundled XML inspector uses only Python's standard library |
| XLSX create/edit/check | `openpyxl`; it does not calculate formulas |
| PDF inspect/extract/merge/select | `pypdf` |
| PDF render | `PyMuPDF` |
| PDF report creation example | `reportlab` |
| Office rendering/conversion or workbook recalculation | A compatible application such as LibreOffice, with suitable fonts |

A parsed file is not necessarily visually correct, and a stored formula is not a calculated result. Skill instructions distinguish structural inspection, actual recalculation and viewed rendering. Original inputs are preserved by default; helper outputs refuse implicit replacement.

For contributors, `uv sync --group skill-tests` prepares the document test libraries and `uv run --group skill-tests pytest tests/extensions/skills -q` exercises real files, examples, resource closure and override behavior. The CI Python matrix runs these tests on Linux and Windows. LibreOffice and native Office visual checks depend on the host and are reported separately from automated library tests.
