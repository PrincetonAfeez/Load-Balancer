"""Documentation consistency checks for the rule DSL schema."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

from load_balancer.rule_dsl import ALLOWED_FIELDS, L4_RULE_FIELDS, compile_rule
from load_balancer.vm import Instruction, OpCode, VMError

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "rule-dsl-schema.md"


class RuleDslSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema_text = SCHEMA_PATH.read_text(encoding="utf-8")

    def test_schema_file_exists(self) -> None:
        self.assertTrue(SCHEMA_PATH.is_file())

    def test_opcode_enum_matches_schema_document(self) -> None:
        enum_names = {member.value for member in OpCode}
        for name in enum_names:
            with self.subTest(opcode=name):
                self.assertIn(f"`{name}`", self.schema_text)
        opcode_block = self.schema_text.split("Complete opcode set", 1)[1]
        documented = set(re.findall(r"`([A-Z_]+)`", opcode_block.split("##", 1)[0]))
        self.assertEqual(documented, enum_names)

    def test_schema_documents_jump_integer_argument(self) -> None:
        self.assertIn("| `JUMP` | integer instruction index |", self.schema_text)
        self.assertIn("does not emit `JUMP`", self.schema_text)
        self.assertNotIn("null or integer instruction index", self.schema_text)

    def test_schema_documents_load_const_compiler_output(self) -> None:
        self.assertIn(
            "string or null from the current DSL compiler",
            self.schema_text,
        )
        self.assertIn(
            "compiler-generated `LOAD_CONST` instructions contain strings",
            self.schema_text,
        )

    def test_schema_documents_contains_list_and_string_only(self) -> None:
        self.assertIn("substring membership for strings", self.schema_text)
        self.assertIn("for lists", self.schema_text)
        self.assertNotIn("list-like", self.schema_text)

    def test_schema_documents_allowed_fields(self) -> None:
        for field in ALLOWED_FIELDS:
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", self.schema_text)
        self.assertIn(
            "`L4_RULE_FIELDS`",
            self.schema_text,
        )
        self.assertEqual(L4_RULE_FIELDS, frozenset({"client.ip", "client.port"}))

    def test_compiler_emits_only_string_or_null_constants(self) -> None:
        program = compile_rule(
            'if client.ip == "10.0.0.1" then return "a" else return "b"'
        )
        const_args = {
            instruction.arg
            for instruction in program
            if instruction.op is OpCode.LOAD_CONST
        }
        self.assertTrue(all(isinstance(value, str) for value in const_args))

        elseless = compile_rule('return "default"')
        elseless_consts = [
            instruction.arg
            for instruction in elseless
            if instruction.op is OpCode.LOAD_CONST
        ]
        self.assertEqual(elseless_consts, ["default"])

        conditional_elseless = compile_rule(
            'if client.ip == "10.0.0.1" then return "match"'
        )
        fallback_consts = [
            instruction.arg
            for instruction in conditional_elseless
            if instruction.op is OpCode.LOAD_CONST
        ]
        self.assertIn("match", fallback_consts)
        self.assertIn(None, fallback_consts)

    def test_jump_rejects_null_argument(self) -> None:
        from load_balancer.vm import VirtualMachine

        program = [
            Instruction(OpCode.LOAD_CONST, "x"),
            Instruction(OpCode.JUMP, None),
            Instruction(OpCode.RETURN),
        ]
        with self.assertRaises(VMError):
            VirtualMachine().execute(program, {})


if __name__ == "__main__":
    unittest.main()
