"""Comprehensive virtual machine tests."""

from __future__ import annotations

import unittest

from load_balancer.errors import VMError
from load_balancer.vm import Instruction, OpCode, VirtualMachine


class VirtualMachineTests(unittest.TestCase):
    def test_startswith_branch(self):
        program = [
            Instruction(OpCode.LOAD_FIELD, "client.ip"),
            Instruction(OpCode.LOAD_CONST, "10."),
            Instruction(OpCode.STARTSWITH),
            Instruction(OpCode.JUMP_IF_FALSE, 6),
            Instruction(OpCode.LOAD_CONST, "match"),
            Instruction(OpCode.RETURN),
            Instruction(OpCode.LOAD_CONST, "nomatch"),
            Instruction(OpCode.RETURN),
        ]
        vm = VirtualMachine()
        self.assertEqual(
            vm.execute(program, {"client": {"ip": "10.0.0.1"}}), "match"
        )
        self.assertEqual(
            vm.execute(program, {"client": {"ip": "192.0.2.1"}}), "nomatch"
        )

    def test_eq_and_boolean_ops(self):
        program = [
            Instruction(OpCode.LOAD_CONST, "a"),
            Instruction(OpCode.LOAD_CONST, "a"),
            Instruction(OpCode.EQ),
            Instruction(OpCode.LOAD_CONST, True),
            Instruction(OpCode.LOAD_CONST, False),
            Instruction(OpCode.OR),
            Instruction(OpCode.RETURN),
        ]
        self.assertTrue(VirtualMachine().execute(program, {}))

    def test_and_op(self):
        program = [
            Instruction(OpCode.LOAD_CONST, True),
            Instruction(OpCode.LOAD_CONST, False),
            Instruction(OpCode.AND),
            Instruction(OpCode.RETURN),
        ]
        self.assertFalse(VirtualMachine().execute(program, {}))

    def test_contains_list_and_string(self):
        vm = VirtualMachine()
        list_prog = [
            Instruction(OpCode.LOAD_FIELD, "tags"),
            Instruction(OpCode.LOAD_CONST, "stable"),
            Instruction(OpCode.CONTAINS),
            Instruction(OpCode.RETURN),
        ]
        self.assertTrue(
            vm.execute(list_prog, {"tags": ["stable", "canary"]})
        )
        str_prog = [
            Instruction(OpCode.LOAD_CONST, "hello"),
            Instruction(OpCode.LOAD_CONST, "ell"),
            Instruction(OpCode.CONTAINS),
            Instruction(OpCode.RETURN),
        ]
        self.assertTrue(vm.execute(str_prog, {}))

    def test_jump_if_false_and_jump(self):
        program = [
            Instruction(OpCode.LOAD_CONST, False),
            Instruction(OpCode.JUMP_IF_FALSE, 4),
            Instruction(OpCode.LOAD_CONST, "skip"),
            Instruction(OpCode.RETURN),
            Instruction(OpCode.LOAD_CONST, "taken"),
            Instruction(OpCode.RETURN),
        ]
        self.assertEqual(VirtualMachine().execute(program, {}), "taken")

    def test_missing_field_compares_as_none(self):
        program = [
            Instruction(OpCode.LOAD_FIELD, "missing.path"),
            Instruction(OpCode.LOAD_CONST, "x"),
            Instruction(OpCode.EQ),
            Instruction(OpCode.RETURN),
        ]
        self.assertFalse(VirtualMachine().execute(program, {}))

    def test_instruction_limit(self):
        program = [
            Instruction(OpCode.LOAD_CONST, 1),
            Instruction(OpCode.JUMP, 0),
        ]
        with self.assertRaises(VMError):
            VirtualMachine(max_instructions=3).execute(program, {})

    def test_stack_underflow(self):
        program = [Instruction(OpCode.RETURN)]
        with self.assertRaises(VMError):
            VirtualMachine().execute(program, {})

    def test_invalid_jump_target(self):
        program = [
            Instruction(OpCode.JUMP, 99),
            Instruction(OpCode.LOAD_CONST, "x"),
            Instruction(OpCode.RETURN),
        ]
        with self.assertRaises(VMError):
            VirtualMachine().execute(program, {})

    def test_unsupported_opcode(self):
        class WeirdOp(str):
            pass

        bad = Instruction(WeirdOp("WEIRD"), "x")  # type: ignore[arg-type]
        with self.assertRaises(VMError):
            VirtualMachine().execute([bad], {})

    def test_no_return_raises(self):
        program = [Instruction(OpCode.LOAD_CONST, "x")]
        with self.assertRaises(VMError):
            VirtualMachine().execute(program, {})

    def test_init_rejects_zero_limit(self):
        with self.assertRaises(ValueError):
            VirtualMachine(max_instructions=0)


if __name__ == "__main__":
    unittest.main()
