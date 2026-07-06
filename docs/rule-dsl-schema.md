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
| `LOAD_FIELD` | dotted field name string |
| `LOAD_CONST` | string, boolean, number, or `null` as produced by the compiler |
| `EQ` | `null` |
| `STARTSWITH` | `null` |
| `CONTAINS` | `null` |
| `AND` | `null` |
| `OR` | `null` |
| `JUMP_IF_FALSE` | integer instruction index |
| `JUMP` | `null` or integer instruction index (reserved; compiler uses `JUMP_IF_FALSE`) |
| `RETURN` | `null` |

Complete opcode set (`OpCode`):

`LOAD_FIELD`, `LOAD_CONST`, `EQ`, `STARTSWITH`, `CONTAINS`, `AND`, `OR`,
`JUMP_IF_FALSE`, `JUMP`, `RETURN`

## Runtime semantics notes

- `EQ` compares the two stack values with `==`.
- `STARTSWITH` requires both operands to be strings.
- `CONTAINS` checks substring membership for strings and element membership for
  list-like left-hand values; other type combinations yield `false`.
- Missing fields loaded by `LOAD_FIELD` become `null` and normally make
  comparisons false.
