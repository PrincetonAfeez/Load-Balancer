# 4. A compiled rule DSL on a bounded stack VM (no `eval`)

Status: Accepted

## Context

Policy routing ("if the client is in 10.0.0.0/8, use consistent hashing") needs
a small expression language. Using Python `eval`/`exec` on config-supplied text
would be an arbitrary-code-execution hole, even for a localhost tool.

## Decision

Implement a tiny DSL compiled through `source -> tokens -> AST -> bytecode` and
executed on a deliberately small, deterministic stack VM. The instruction set is
fixed: `LOAD_FIELD`, `LOAD_CONST`, `EQ`, `STARTSWITH`, `CONTAINS`, `AND`,
`OR`, `JUMP_IF_FALSE`, `JUMP`, and `RETURN`. Fields are whitelisted; there is
no `eval`, no attribute access, and no arbitrary Python.

## Consequences

- Execution is bounded by a configurable instruction limit. Because compiled
  programs only jump forward, the executed-instruction count can never exceed
  the program length, so an over-limit program is rejected at *config-load* time
  rather than failing every connection at runtime.
- Unsupported fields and syntax errors fail validation at load/reload, so a bad
  rule keeps the previous good config running.
- The VM is pure and synchronous, so it runs safely inside backend selection.
- The language intentionally supports a small set of string and membership
  operations: `==`, `startswith`, `contains`, `and`, `or`, and parentheses.
  Fields remain whitelisted, and no arbitrary Python evaluation or attribute
  access is allowed.
- `contains` checks substring membership for strings and element membership for
  supported list-like tag values. Missing fields resolve to no value and
  normally produce a false comparison.
- On the Layer 4 connection path, only `client.ip` and `client.port` are
  populated. Other allowed fields compile but are not populated unless a future
  routing context supplies them.

See also [rule-dsl-schema.md](../rule-dsl-schema.md) for maintainer-facing token,
AST, and bytecode structure notes.
