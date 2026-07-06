# 4. A compiled rule DSL on a bounded stack VM (no `eval`)

Status: Accepted

## Context

Policy routing ("if the client is in 10.0.0.0/8, use consistent hashing") needs
a small expression language. Using Python `eval`/`exec` on config-supplied text
would be an arbitrary-code-execution hole, even for a localhost tool.

## Decision

Implement a tiny DSL compiled through `source -> tokens -> AST -> bytecode` and
executed on a deliberately small, deterministic stack VM. The instruction set is
fixed (`LOAD_FIELD`, `LOAD_CONST`, `EQ`, `STARTSWITH`, `AND`, `OR`,
`JUMP_IF_FALSE`, `JUMP`, `RETURN`). Fields are whitelisted; there is no `eval`,
no attribute access, and no arbitrary Python.

## Consequences

- Execution is bounded by a configurable instruction limit. Because compiled
  programs only jump forward, the executed-instruction count can never exceed
  the program length, so an over-limit program is rejected at *config-load* time
  rather than failing every connection at runtime.
- Unsupported fields and syntax errors fail validation at load/reload, so a bad
  rule keeps the previous good config running.
- The VM is pure and synchronous, so it runs safely inside backend selection.
- Trade-off: the language is intentionally minimal (string `==`/`startswith`,
  `and`/`or`, parentheses) — expressive enough for routing policy, small enough
  to audit. At L4 only `client.*` fields are populated.
