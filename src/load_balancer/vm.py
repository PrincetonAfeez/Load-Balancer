"""A deliberately small, deterministic stack virtual machine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import VMError


class OpCode(StrEnum):
    LOAD_FIELD = "LOAD_FIELD"
    LOAD_CONST = "LOAD_CONST"
    EQ = "EQ"
    STARTSWITH = "STARTSWITH"
    CONTAINS = "CONTAINS"
    AND = "AND"
    OR = "OR"
    JUMP_IF_FALSE = "JUMP_IF_FALSE"
    JUMP = "JUMP"
    RETURN = "RETURN"


@dataclass(frozen=True, slots=True)
class Instruction:
    op: OpCode
    arg: Any = None


def _get_field(context: Mapping[str, Any], path: str) -> Any:
    value: Any = context
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


class VirtualMachine:
    def __init__(self, max_instructions: int = 256) -> None:
        if max_instructions <= 0:
            raise ValueError("max_instructions must be positive")
        self.max_instructions = max_instructions

    def execute(
        self, program: Sequence[Instruction], context: Mapping[str, Any]
    ) -> Any:
        stack: list[Any] = []
        ip = 0
        executed = 0
        while 0 <= ip < len(program):
            executed += 1
            if executed > self.max_instructions:
                raise VMError("rule exceeded the instruction limit")
            instruction = program[ip]
            ip += 1

            if instruction.op is OpCode.LOAD_FIELD:
                stack.append(_get_field(context, str(instruction.arg)))
            elif instruction.op is OpCode.LOAD_CONST:
                stack.append(instruction.arg)
            elif instruction.op is OpCode.EQ:
                right, left = self._pop2(stack)
                stack.append(left == right)
            elif instruction.op is OpCode.STARTSWITH:
                right, left = self._pop2(stack)
                stack.append(
                    isinstance(left, str)
                    and isinstance(right, str)
                    and left.startswith(right)
                )
            elif instruction.op is OpCode.CONTAINS:
                right, left = self._pop2(stack)
                if isinstance(left, list):
                    stack.append(isinstance(right, str) and right in left)
                elif isinstance(left, str) and isinstance(right, str):
                    stack.append(right in left)
                else:
                    stack.append(False)
            elif instruction.op is OpCode.AND:
                right, left = self._pop2(stack)
                stack.append(bool(left) and bool(right))
            elif instruction.op is OpCode.OR:
                right, left = self._pop2(stack)
                stack.append(bool(left) or bool(right))
            elif instruction.op is OpCode.JUMP_IF_FALSE:
                if not stack:
                    raise VMError("stack underflow on JUMP_IF_FALSE")
                if not bool(stack.pop()):
                    ip = self._jump_target(instruction.arg, len(program))
            elif instruction.op is OpCode.JUMP:
                ip = self._jump_target(instruction.arg, len(program))
            elif instruction.op is OpCode.RETURN:
                if not stack:
                    raise VMError("stack underflow on RETURN")
                return stack.pop()
            else:
                raise VMError(f"unsupported opcode: {instruction.op}")
        raise VMError("program terminated without RETURN")

    @staticmethod
    def _pop2(stack: list[Any]) -> tuple[Any, Any]:
        if len(stack) < 2:
            raise VMError("stack underflow")
        return stack.pop(), stack.pop()

    @staticmethod
    def _jump_target(value: Any, program_size: int) -> int:
        if not isinstance(value, int) or not 0 <= value < program_size:
            raise VMError(f"invalid jump target: {value!r}")
        return value

