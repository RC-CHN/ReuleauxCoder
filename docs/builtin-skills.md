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

## Optional presentation metadata

The VS Code skill panel reads the core's discovered catalog, with no frontend list of builtin names. It supports source filters, localized search, separate details and enable switches, and native instruction documents. Builtin documents open read-only. **Copy to workspace** copies the complete directory, including references, scripts and licenses, reloads discovery and opens the editable local variant. Existing directories are never overwritten; symbolic links are rejected. Global edits affect other workspaces using that global skill. Save edits before reloading skills.

Ordinary skills require only the existing `name` and `description`. The following **optional rcoder extension keys** customize presentation; they are not required skill fields and are not assumed to be recognized by other clients:

```yaml
name: example
description: Use to perform the task described by this skill.
metadata:
  rcoder.display-name: Example workflow
  rcoder.display-name.zh-CN: 示例工作流
  rcoder.summary: A short explanation for people browsing skills
  rcoder.summary.zh-CN: 供用户浏览时阅读的简短说明
  rcoder.icon: document
  rcoder.category: documents
```

`name` remains the stable identifier used by actions and configuration; `description` remains the model-facing trigger. Display metadata does not enter the LLM catalog. `scope` comes from core discovery, not frontmatter; a skill cannot declare itself builtin. A custom override supplies its own metadata and does not inherit the builtin's branding.

| Optional field | Behavior and fallback |
| --- | --- |
| `rcoder.display-name` | English display title, up to 120 characters; defaults to `name` |
| `rcoder.summary` | English UI summary, up to 500 characters; defaults to `description` |
| Either text key with a locale suffix, such as `.zh-CN` | Locale keys are case insensitive; Chinese UI tries `zh-cn`, then `zh`, then English, then the standard field; English UI uses English or the standard field |
| `rcoder.icon` | A symbolic icon name; an absent or unknown icon uses the generic skill icon. Examples: `skills`, `settings`, `shield`, `commands`, `document`, `presentation`, `table`, `pen`, `terminal`, `attach` |
| `rcoder.category` | `development`, `documents`, `writing` or `configuration`; absent/unknown values appear under Other skills |

Metadata values use strings. Missing metadata is normal. Invalid optional values are ignored independently with a warning, so valid fields still work and the skill remains usable and toggleable. Unrelated vendor metadata is ignored. Malformed YAML or missing required `name`/`description` still invalidates the skill. Display-only edits are recognized on reload. Older cores without the structured panel details keep the existing basic skill list; the UI does not guess source or file access from a display name.

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
