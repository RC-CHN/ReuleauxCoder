---
name: project-guide
metadata:
  rcoder.display-name: "Project guidance"
  rcoder.display-name.zh-CN: "项目指南"
  rcoder.summary: "Maintain project instructions and verified development commands"
  rcoder.summary.zh-CN: "维护项目指引与经过验证的开发命令"
  rcoder.icon: "document"
  rcoder.category: "development"
description: Initialize or maintain project instructions using verified architecture, commands and conventions.
---

# Maintain project instructions

Use this for initialization, stale project guidance or durable conventions. Find existing `AGENT.md`, `AGENTS.md` or user-selected instructions, including relevant directory rules. Update an existing owner rather than creating competing copies.

rcoder reads existing `AGENT.md`, `AGENTS.md`, `.agent.md`, `CLAUDE.md` and `.claude.md` in that order from its working directory. Finding the first file does not suppress the others. Check for duplication before adding a new file.

Verify facts against READMEs, manifests, scripts, CI and representative code:

- Product purpose, stack and real entry points.
- Build, test, lint and run commands, including required environment.
- Main modules, dependency direction and resource ownership.
- Conventions, compatibility promises and non-obvious constraints.

Trace commands to their actual definitions. Distinguish commands inspected from commands executed. Do not infer new mandatory conventions from a small sample or turn a temporary debugging result into a permanent rule.

Merge duplicate rules, repair stale paths and retire superseded facts while preserving unique rationale and invariants. Keep the entry concise; link detailed domain documentation to its existing owner. Exclude temporary plans, progress reports, generated outputs, secrets and conversation history.

Check paths, commands and consistency with other instructions. Report changed facts and unresolved questions. A review request authorizes findings, not automatic edits.
