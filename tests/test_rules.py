""" Test the rules module """

from __future__ import annotations

import unittest

from load_balancer.errors import RuleSyntaxError, VMError
from load_balancer.rule_dsl import (
    TokenKind,
    compile_rule,
    non_l4_fields_in_rule,
    parse,
    tokenize,
)
from load_balancer.vm import Instruction, OpCode, VirtualMachine


class RuleTests(unittest.TestCase):
    def test_tokenizer_parser_compiler_vm(self):
        source = (
            'if client.ip startswith "10." and command == "status" '
            'then return "consistent_hash" else return "round_robin"'
        )
        tokens = tokenize(source)
        self.assertEqual(tokens[0].kind, TokenKind.IF)
        self.assertIsNotNone(parse(source).condition)
        program = compile_rule(source)
        vm = VirtualMachine()
        result = vm.execute(
            program, {"client": {"ip": "10.2.3.4"}, "command": "status"}
        )
        self.assertEqual(result, "consistent_hash")
        result = vm.execute(
            program, {"client": {"ip": "192.0.2.1"}, "command": "status"}
        )
        self.assertEqual(result, "round_robin")

    def test_non_l4_fields_detected(self):
        source = (
            'if client.ip startswith "10." and command == "status" '
            'then return "consistent_hash"'
        )
        self.assertEqual(non_l4_fields_in_rule(source), ["command"])
        self.assertEqual(non_l4_fields_in_rule('return "default"'), [])

    def test_invalid_syntax_and_field(self):
        with self.assertRaises(RuleSyntaxError):
            compile_rule('if client.ip == then return "x"')
        with self.assertRaises(RuleSyntaxError):
            compile_rule('if secret.value == "x" then return "x"')

    def test_parser_edge_cases(self):
        # Empty / whitespace-only source.
        with self.assertRaises(RuleSyntaxError):
            compile_rule("")
        with self.assertRaises(RuleSyntaxError):
            compile_rule("   \n\t ")
        # Unterminated string literal.
        with self.assertRaises(RuleSyntaxError):
            compile_rule('return "unterminated')
        # Trailing backslash -> unterminated escape inside a string.
        with self.assertRaises(RuleSyntaxError):
            compile_rule('return "bad\\')
        # Unexpected character.
        with self.assertRaises(RuleSyntaxError):
            compile_rule("return %")
        # Valid escapes are decoded, not rejected.
        program = compile_rule(r'if client.ip == "a\tb" then return "round_robin"')
        result = VirtualMachine().execute(program, {"client": {"ip": "a\tb"}})
        self.assertEqual(result, "round_robin")

    def test_instruction_limit(self):
        program = [
            Instruction(OpCode.LOAD_CONST, True),
            Instruction(OpCode.JUMP, 0),
        ]
        with self.assertRaises(VMError):
            VirtualMachine(max_instructions=5).execute(program, {})


if __name__ == "__main__":
    unittest.main()
