"""Tokenizer, parser, and compiler for the tiny policy DSL

Grammar:
    program     := "return" STRING
                 | "if" expression "then" "return" STRING
                   ["else" "return" STRING]
    expression  := or_expression
    or_expression := and_expression ("or" and_expression)*
    and_expression := comparison ("and" comparison)*
    comparison  := IDENT ("==" | "startswith" | "contains") STRING | "(" expression ")"
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re

from .errors import RuleSyntaxError
from .vm import Instruction, OpCode

ALLOWED_FIELDS = {
    "client.ip",
    "client.port",
    "backend.tag",
    "backend.tags",
    "command",
    "args.backend_id",
}

# Facts populated on the L4 connection hot path; other allowed fields resolve
# to None unless a future admin/L7 router supplies them.
L4_RULE_FIELDS = frozenset({"client.ip", "client.port"})


class TokenKind(StrEnum):
    IDENT = "IDENT"
    STRING = "STRING"
    EQ = "EQ"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    IF = "IF"
    THEN = "THEN"
    ELSE = "ELSE"
    RETURN = "RETURN"
    STARTSWITH = "STARTSWITH"
    CONTAINS = "CONTAINS"
    AND = "AND"
    OR = "OR"
    EOF = "EOF"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str
    position: int


KEYWORDS = {
    "if": TokenKind.IF,
    "then": TokenKind.THEN,
    "else": TokenKind.ELSE,
    "return": TokenKind.RETURN,
    "startswith": TokenKind.STARTSWITH,
    "contains": TokenKind.CONTAINS,
    "and": TokenKind.AND,
    "or": TokenKind.OR,
}


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("==", index):
            tokens.append(Token(TokenKind.EQ, "==", index))
            index += 2
            continue
        if char == "(":
            tokens.append(Token(TokenKind.LPAREN, char, index))
            index += 1
            continue
        if char == ")":
            tokens.append(Token(TokenKind.RPAREN, char, index))
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            start = index
            index += 1
            value: list[str] = []
            while index < len(source) and source[index] != quote:
                if source[index] == "\\":
                    index += 1
                    if index >= len(source):
                        raise RuleSyntaxError(f"unterminated escape at {start}")
                    escapes = {"n": "\n", "t": "\t", "\\": "\\", quote: quote}
                    value.append(escapes.get(source[index], source[index]))
                else:
                    value.append(source[index])
                index += 1
            if index >= len(source):
                raise RuleSyntaxError(f"unterminated string at {start}")
            index += 1
            tokens.append(Token(TokenKind.STRING, "".join(value), start))
            continue
        match = re.match(r"[A-Za-z_][A-Za-z0-9_.-]*", source[index:])
        if match:
            word = match.group(0)
            kind = KEYWORDS.get(word.lower(), TokenKind.IDENT)
            tokens.append(Token(kind, word, index))
            index += len(word)
            continue
        raise RuleSyntaxError(f"unexpected character {char!r} at {index}")
    tokens.append(Token(TokenKind.EOF, "", len(source)))
    return tokens


class Expression:
    pass


@dataclass(frozen=True, slots=True)
class Comparison(Expression):
    field: str
    operator: str
    value: str


@dataclass(frozen=True, slots=True)
class Binary(Expression):
    operator: str
    left: Expression
    right: Expression


@dataclass(frozen=True, slots=True)
class Program:
    condition: Expression | None
    true_value: str
    false_value: str | None = None


class Parser:
    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def consume(self, kind: TokenKind) -> Token:
        token = self.current
        if token.kind is not kind:
            raise RuleSyntaxError(
                f"expected {kind.value} at {token.position}, got {token.kind.value}"
            )
        self.index += 1
        return token

    def parse(self) -> Program:
        if self.current.kind is TokenKind.RETURN:
            self.consume(TokenKind.RETURN)
            value = self.consume(TokenKind.STRING).value
            self.consume(TokenKind.EOF)
            return Program(None, value)
        self.consume(TokenKind.IF)
        condition = self.parse_or()
        self.consume(TokenKind.THEN)
        self.consume(TokenKind.RETURN)
        true_value = self.consume(TokenKind.STRING).value
        false_value = None
        if self.current.kind is TokenKind.ELSE:
            self.consume(TokenKind.ELSE)
            self.consume(TokenKind.RETURN)
            false_value = self.consume(TokenKind.STRING).value
        self.consume(TokenKind.EOF)
        return Program(condition, true_value, false_value)

    def parse_or(self) -> Expression:
        expression = self.parse_and()
        while self.current.kind is TokenKind.OR:
            self.consume(TokenKind.OR)
            expression = Binary("or", expression, self.parse_and())
        return expression

    def parse_and(self) -> Expression:
        expression = self.parse_comparison()
        while self.current.kind is TokenKind.AND:
            self.consume(TokenKind.AND)
            expression = Binary("and", expression, self.parse_comparison())
        return expression

    def parse_comparison(self) -> Expression:
        if self.current.kind is TokenKind.LPAREN:
            self.consume(TokenKind.LPAREN)
            expression = self.parse_or()
            self.consume(TokenKind.RPAREN)
            return expression
        field = self.consume(TokenKind.IDENT).value
        if field not in ALLOWED_FIELDS:
            raise RuleSyntaxError(f"unsupported field: {field}")
        if self.current.kind is TokenKind.EQ:
            self.consume(TokenKind.EQ)
            operator = "eq"
        elif self.current.kind is TokenKind.STARTSWITH:
            self.consume(TokenKind.STARTSWITH)
            operator = "startswith"
        elif self.current.kind is TokenKind.CONTAINS:
            self.consume(TokenKind.CONTAINS)
            operator = "contains"
        else:
            raise RuleSyntaxError(
                f"expected comparison operator at {self.current.position}"
            )
        value = self.consume(TokenKind.STRING).value
        return Comparison(field, operator, value)


def parse(source: str) -> Program:
    if not source.strip():
        raise RuleSyntaxError("rule source is empty")
    return Parser(tokenize(source)).parse()


def _compile_expression(expression: Expression) -> Iterator[Instruction]:
    if isinstance(expression, Comparison):
        yield Instruction(OpCode.LOAD_FIELD, expression.field)
        yield Instruction(OpCode.LOAD_CONST, expression.value)
        yield Instruction(
            OpCode.EQ
            if expression.operator == "eq"
            else OpCode.STARTSWITH
            if expression.operator == "startswith"
            else OpCode.CONTAINS
        )
    elif isinstance(expression, Binary):
        yield from _compile_expression(expression.left)
        yield from _compile_expression(expression.right)
        yield Instruction(OpCode.AND if expression.operator == "and" else OpCode.OR)
    else:
        raise RuleSyntaxError(f"unsupported expression node: {type(expression).__name__}")


def compile_program(program: Program) -> list[Instruction]:
    if program.condition is None:
        return [
            Instruction(OpCode.LOAD_CONST, program.true_value),
            Instruction(OpCode.RETURN),
        ]
    instructions = list(_compile_expression(program.condition))
    jump_false_index = len(instructions)
    instructions.append(Instruction(OpCode.JUMP_IF_FALSE, None))
    instructions.append(Instruction(OpCode.LOAD_CONST, program.true_value))
    instructions.append(Instruction(OpCode.RETURN))
    false_target = len(instructions)
    instructions[jump_false_index] = Instruction(OpCode.JUMP_IF_FALSE, false_target)
    # With no `else`, false_value is None: the program returns None, which the
    # caller treats as "not a strategy name" and falls back to the configured
    # strategy. This keeps an else-less rule meaning "use the default otherwise".
    instructions.append(Instruction(OpCode.LOAD_CONST, program.false_value))
    instructions.append(Instruction(OpCode.RETURN))
    return instructions


def compile_rule(source: str) -> list[Instruction]:
    return compile_program(parse(source))


def non_l4_fields_in_rule(source: str) -> list[str]:
    """Return sorted rule fields that are not populated on the L4 proxy path."""
    if not source.strip():
        return []
    seen: set[str] = set()
    for token in tokenize(source):
        if token.kind is TokenKind.IDENT and token.value in ALLOWED_FIELDS:
            if token.value not in L4_RULE_FIELDS:
                seen.add(token.value)
    return sorted(seen)


def rule_can_override_strategy(source: str) -> bool:
    """True when the rule may return a strategy other than the configured default."""
    program = parse(source)
    if program.condition is not None:
        return True
    return program.true_value != "default"

