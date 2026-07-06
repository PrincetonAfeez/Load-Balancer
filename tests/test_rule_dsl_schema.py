"""Documentation consistency checks for the rule DSL schema."""

from __future__ import annotations

import unittest

from load_balancer.rule_dsl import ALLOWED_FIELDS, L4_RULE_FIELDS, Comparison, TokenKind
from load_balancer.vm import OpCode

DOCUMENTED_OPCODES = frozenset(
    {
        "LOAD_FIELD",
        "LOAD_CONST",
        "EQ",
        "STARTSWITH",
        "CONTAINS",
        "AND",
        "OR",
        "JUMP_IF_FALSE",
        "JUMP",
        "RETURN",
    }
)

DOCUMENTED_COMPARISON_OPERATORS = frozenset({"eq", "startswith", "contains"})


class RuleDslSchemaTests(unittest.TestCase):
    def test_opcode_enum_matches_documented_set(self):
        self.assertEqual(frozenset(member.value for member in OpCode), DOCUMENTED_OPCODES)

    def test_token_kind_includes_contains(self):
        self.assertIn(TokenKind.CONTAINS, TokenKind)

    def test_allowed_fields_match_readme_contract(self):
        expected = {
            "client.ip",
            "client.port",
            "backend.tag",
            "backend.tags",
            "command",
            "args.backend_id",
        }
        self.assertEqual(ALLOWED_FIELDS, expected)
        self.assertEqual(L4_RULE_FIELDS, frozenset({"client.ip", "client.port"}))

    def test_comparison_operators_are_documented(self):
        for operator in DOCUMENTED_COMPARISON_OPERATORS:
            node = Comparison("client.ip", operator, "x")
            self.assertEqual(node.operator, operator)


if __name__ == "__main__":
    unittest.main()
