---
name: code-review
metadata:
  rcoder.display-name: "Code review"
  rcoder.display-name.zh-CN: "代码审查"
  rcoder.summary: "Check correctness, concurrency and security risks"
  rcoder.summary.zh-CN: "检查正确性、并发问题与安全风险"
  rcoder.icon: "shield"
  rcoder.category: "development"
description: Review changes for correctness, deadlocks, security, compatibility and missing tests using concrete evidence.
---

# Review changes

Honor the requested commit range, files or concern. Otherwise inspect working-tree status, staged/unstaged changes, branch and comparison base. Distinguish committed changes from local edits. Reviewing is read-only unless the user also requested fixes.

## Establish evidence

Read the diff and surrounding behavior, then verify callers, error paths and lifecycle ownership. A definition lookup does not prove there are no callers: use available LSP references or content search. State search scope for negative claims.

Prioritize:

- Input through state transitions, output and persistence.
- Lock acquisition order, I/O or callbacks under locks, reentrancy, and cross-thread/async wait dependencies.
- Cancellation, failure, retries and shutdown: who releases resources, settles waiters and prevents duplicate effects?
- Duplicate state across modules, inverted ownership and abstractions that expose implementation details.
- CLI/RPC, configuration, saved sessions, file formats and extension compatibility.
- Tests covering real triggers rather than implementation-shaped mocks.

For untrusted input, permissions or sensitive data, read [references/security.md](references/security.md). Explain responsibility changes and preserved behavior in decoupling recommendations; do not turn a review into a redesign.

## Report

Lead with substantive findings, highest impact first. Include location, trigger, consequence, evidence and a concrete fix direction. Severity depends on reachability and impact, not a fixed label per bug category. Distinguish reproduction, code-derived conclusions and unverified risks.

Do not manufacture findings or report style preferences as defects. If clean, describe coverage and unperformed checks rather than claiming the absence of every possible race or vulnerability.
