# Rule DSL internal schema

This document describes the tokenizer, parser, and compiler structures used by
`load_balancer.rule_dsl` and the bytecode executed by `load_balancer.vm`.

## Stability

The DSL **source grammar** and **runtime behavior** are supported interfaces.
Token, AST, and bytecode objects are documented here for maintainers and tests
but remain **internal implementation details** unless explicitly promoted to a
public API.

## Token schema

```text
Token
  kind: TokenKind
  value: str
  position: int   # zero-based source character offset
```

`TokenKind` values:

| Kind | Meaning |
|------|---------|
| `IDENT` | Identifier or dotted field name |
| `STRING` | Quoted string literal |
| `EQ` | `==` |
| `LPAREN` | `(` |
| `RPAREN` | `)` |
| `IF` | keyword `if` |
| `THEN` | keyword `then` |
| `ELSE` | keyword `else` |
| `RETURN` | keyword `return` |
| `STARTSWITH` | keyword `startswith` |
| `CONTAINS` | keyword `contains` |
| `AND` | keyword `and` |
| `OR` | keyword `or` |
| `EOF` | end of input |

## AST schema

```text
Program
  condition: Comparison | Binary | null
  true_value: str
  false_value: str | null

Comparison
  field: str            # one of ALLOWED_FIELDS
  operator: "eq" | "startswith" | "contains"
  value: str

Binary
  operator: "and" | "or"
  left: Comparison | Binary
  right: Comparison | Binary
```

Allowed field names (`ALLOWED_FIELDS`):

- `client.ip`
- `client.port`
- `backend.tag`
- `backend.tags`
- `command`
- `args.backend_id`

On the Layer 4 proxy path, only `client.ip` and `client.port` are populated
(`L4_RULE_FIELDS`). Other fields compile but resolve to no value unless a future
router supplies them.

## Bytecode schema

```text
Instruction
  op: OpCode
  arg: Any | null
```

## Opcode argument table

| Opcode | `arg` |
|--------|-------|
| `LOAD_FIELD` | dotted field-name string |
| `LOAD_CONST` | string or null from the current DSL compiler; arbitrary values are possible in manually authored VM programs |
| `EQ` | null |
| `STARTSWITH` | null |
| `CONTAINS` | null |
| `AND` | null |
| `OR` | null |
| `JUMP_IF_FALSE` | integer instruction index |
| `JUMP` | integer instruction index |
| `RETURN` | null |

The current DSL compiler does not emit `JUMP`, but the VM supports it for
manually constructed bytecode and tests. Its argument must still be a valid
integer instruction index.

The current source grammar contains only quoted string literals. Therefore,
compiler-generated `LOAD_CONST` instructions contain strings, except for null
used as the fallback result of an else-less rule. The VM itself accepts
arbitrary constant values in manually authored `Instruction` objects.

Complete opcode set (`OpCode`):

`LOAD_FIELD`, `LOAD_CONST`, `EQ`, `STARTSWITH`, `CONTAINS`, `AND`, `OR`,
`JUMP_IF_FALSE`, `JUMP`, `RETURN`

## Runtime semantics notes

- `EQ` compares the two stack values using `==`.
- `STARTSWITH` evaluates to true only when both operands are strings and the
  left operand starts with the right operand.
- `CONTAINS` performs substring membership for strings and element membership
  for lists. Other operand combinations evaluate to false.
- `LOAD_FIELD` returns null for a missing path.
- `JUMP` and `JUMP_IF_FALSE` require an in-range integer instruction target.
- The current DSL compiler emits string constants and null only; manually
  constructed VM programs may use additional constant types.
