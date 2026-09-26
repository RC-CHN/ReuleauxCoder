---
name: skill-creator
metadata:
  rcoder.display-name: "Skill authoring"
  rcoder.display-name.zh-CN: "技能编写"
  rcoder.summary: "Turn a repeatable workflow into a self-contained skill"
  rcoder.summary.zh-CN: "把可复用的工作流程写成独立技能"
  rcoder.icon: "skills"
  rcoder.category: "development"
description: Create or improve a skill, or capture a repeatable workflow from the current conversation.
---

# Create effective skills

Extract the outcome, trigger, inputs, deliverables and user corrections from the request and conversation. Ask only for missing information that would change the result. Write instructions in English by default; preserve a user's explicit language choice.

## Scope and structure

- Project skills belong in `<workspace>/.rcoder/skills/<name>/`; reusable personal skills belong in `~/.rcoder/skills/<name>/` on the core host. Follow the scope already chosen by the user.
- Use lowercase words/numbers separated by hyphens. The directory and frontmatter `name` must match. A `SKILL.md` needs YAML `name` and `description`, followed by the instructions.
- Optional rcoder UI hints go in frontmatter `metadata` as string values: `rcoder.display-name`, `rcoder.summary`, their `.zh-CN` translations, `rcoder.icon` and `rcoder.category`. They are display extensions, not required skill fields. Omit them when unnecessary; ordinary name/description still work. Invalid hints fall back independently, and source identity comes from discovery. Keep instructions and the model-facing description useful without these hints.
- Describe when the skill applies precisely. rcoder does not use `allowed-tools`, `context: fork` or `agents/openai.yaml` to enforce permissions or orchestrate agents.
- Keep decision-changing domain knowledge, meaningful constraints and verification steps. Avoid generic advice, conversation transcripts, secrets and one-off paths.
- Put task-specific detail in local `references/` files linked from the relevant workflow. Add `scripts/` only for repeated or fragile operations; `assets/` hold output templates.
- Each skill must work with its own directory and documented external dependencies. Do not link to sibling skills, development checkouts or shared script directories.
- Improve existing skills in place, preserving valid constraints. A skill cannot expand authorization or require unavailable tools, proprietary memory services or a fixed number of subagents.

## Capture an existing workflow

Keep the steps that actually worked, relevant corrections, input/output contracts and evidence of completion. Remove abandoned attempts. Turn incidental names into discoverable parameters while preserving important prerequisites. Explain failures that change the next decision, rather than narrating the entire session.

## Validate and deliver

Run [scripts/validate_skill.py](scripts/validate_skill.py) with the skill directory using the core's Python. Then check a normal request, an edge case and a nearby request that should not trigger the skill. Run new scripts on disposable fixtures and inspect their outputs.

When authorized and available, test with a real agent task. Clearly distinguish a manual scenario review from an actual model evaluation. Do not claim behavioral success from frontmatter validation alone.

Refresh through `/skills reload` in the running conversation and inspect `/skills` for name, scope and disabled state. These are session commands, not shell subcommands. Paths in the catalog belong to the core host; confirm or transfer resources before using a shell routed to another host.
