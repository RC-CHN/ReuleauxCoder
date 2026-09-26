# Security review

Trace data from actual entry points to sensitive operations: caller, input control, parsing, authorization and execution host. The presence of shell commands, paths or tokens is not itself a vulnerability.

Inspect command/query construction, traversal and symlinks, identity and object-level authorization, outbound request targets, deserialization, credential logging, output encoding and check/use races. For async work, also inspect post-cancellation effects, idempotency and whether the approved operation still matches execution.

Give reachable prerequisites and the data flow for every finding. Use minimal local examples or tests; do not probe third-party systems without authorization. Address the cause while preserving legitimate behavior, rather than disabling functionality or swallowing errors.

Prefer established cryptography and credential handling. Record secret locations/types without repeating their values. Validate scanner findings, and do not equate an empty search with absence of risk.
