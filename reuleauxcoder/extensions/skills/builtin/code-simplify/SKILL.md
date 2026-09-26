---
name: code-simplify
description: Simplify code by removing duplicated state, unnecessary abstractions and coupling while preserving behavior.
---

# Simplify and decouple

Respect the requested scope and distinguish an analysis from an authorized refactor. Establish observable behavior and protected invariants before removing code. Fewer lines do not justify losing compatibility or recovery behavior.

For each candidate, identify who creates, reads, changes and disposes it. Search production callers separately from tests and documentation. Look for:

- State, caches, observers or events representing the same fact.
- Unused parameters, public methods, registries and compatibility branches.
- Duplicated logic or shared layers that require knowledge of caller internals.
- Functions mixing domain rules, parsing, persistence and presentation.
- Competing completion/cancellation/disposal flags for one lifecycle.
- Repeated hot-path work, notifications with no change, unbounded caches and leaked listeners.

Choose boundaries by ownership and reasons to change. A few adjacent error checks need not become a class. Distinct cancellation, durability, authorization or transaction owners may require separate mechanisms.

An existing library or standard facility can reduce maintenance, but account for dependencies, remaining glue and semantic differences. Verify real callers before declaring an API unused.

Implement a small, explainable improvement using existing mechanisms. Remove obsolete paths and update their consumers. Each mutable fact should have a clear owner. Validate relevant failure, cancellation, restart, lock-order and old-data paths. If behavior must change, call it out as a product decision.

Report what was removed or consolidated, what verified behavior and which boundaries remain. Avoid unrelated formatting churn.
