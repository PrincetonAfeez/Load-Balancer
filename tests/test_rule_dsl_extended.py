"""Extended rule DSL tests."""

from __future__ import annotations

import unittest

from load_balancer.errors import RuleSyntaxError
from load_balancer.rule_dsl import (
    compile_program,
    compile_rule,
    parse,
    rule_can_override_strategy,
    tokenize,
)
from load_balancer.vm import VirtualMachine


class RuleDslExtendedTests(unittest.TestCase):
    def test_simple_return_default(self):
        program = compile_rule('return "default"')
        self.assertEqual(VirtualMachine().execute(program, {}), "default")

    def test_or_expression(self):
        source = (
            'if client.ip == "1" or client.ip == "2" then return "round_robin" '
            'else return "default"'
        )
        program = compile_rule(source)
        self.assertEqual(
            VirtualMachine().execute(program, {"client": {"ip": "2"}}),
            "round_robin",
        )

    def test_contains_backend_tags(self):
        source = 'if backend.tags contains "stable" then return "round_robin"'
        program = compile_rule(source)
        result = VirtualMachine().execute(
            program, {"backend": {"tags": ["stable"]}}
        )
        self.assertEqual(result, "round_robin")

    def test_parenthesized_expression(self):
        source = (
            'if (client.ip == "10.0.0.1") then return "least_connections"'
        )
        program = compile_rule(source)
        self.assertEqual(
            VirtualMachine().execute(program, {"client": {"ip": "10.0.0.1"}}),
            "least_connections",
        )

    def test_if_without_else(self):
        program = compile_rule('if client.ip == "x" then return "round_robin"')
        self.assertIsNone(
            VirtualMachine().execute(program, {"client": {"ip": "y"}})
        )

    def test_rule_can_override_strategy(self):
        self.assertFalse(rule_can_override_strategy('return "default"'))
        self.assertTrue(rule_can_override_strategy('return "round_robin"'))

    def test_tokenize_operators(self):
        tokens = tokenize('client.ip == "a"')
        kinds = [t.kind.value for t in tokens]
        self.assertIn("EQ", kinds)

    def test_parse_return_only(self):
        program = parse('return "x"')
        self.assertIsNone(program.condition)
        self.assertEqual(program.true_value, "x")

    def test_compile_program_direct(self):
        program = parse('return "consistent_hash"')
        instructions = compile_program(program)
        self.assertTrue(instructions)

    def test_invalid_field_rejected(self):
        with self.assertRaises(RuleSyntaxError):
            compile_rule('if os.path == "x" then return "x"')

    def test_unclosed_parenthesis(self):
        with self.assertRaises(RuleSyntaxError):
            compile_rule('if (client.ip == "x" then return "x"')


if __name__ == "__main__":
    unittest.main()
