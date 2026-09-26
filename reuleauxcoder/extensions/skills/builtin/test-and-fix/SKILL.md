---
name: test-and-fix
description: Run tests, diagnose failures, fix their causes and verify results without weakening assertions.
---

# Diagnose and fix tests

Find the actual commands, working directory, runtime versions and prerequisites in project instructions, manifests, test configuration and CI. Follow the lockfile and project runner. A language alone does not identify the test framework.

Reproduce the smallest relevant failure first. Record command, exit status and meaningful error. For long jobs, use supported process/wait facilities and observable completion rather than fixed sleeps.

| Cause | Action |
| --- | --- |
| Product defect | Trace real callers, fix the cause, rerun the trigger |
| Incorrect test | Verify intended behavior and explain the wrong assertion or fixture before changing it |
| Environment, service or permission | Identify the missing prerequisite and prepare it within existing authorization |
| Intermittent or timing failure | Preserve logs/seed, inspect resource and timing dependencies; one green rerun is not proof |

Do not delete assertions, broaden skips, swallow exceptions or rerun until a failure happens to disappear. Each change needs new evidence. Repeated failure of the same hypothesis calls for a new diagnosis or a concrete blocker, not an arbitrary attempt counter.

After a fix, rerun the original failure and nearby regressions, then the required checks appropriate to the impact. Low-risk edits do not require invented tests.

Report actual passes, failures, skips and unperformed checks. A successful process exit is insufficient if no tests were collected or the relevant scenario was skipped.
