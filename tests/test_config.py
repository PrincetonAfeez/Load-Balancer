""" Test the config parser """

from __future__ import annotations

import unittest

from load_balancer.config_parser import parse_config_data
from load_balancer.errors import ConfigError, RuleSyntaxError


def valid_data():
    return {
        "listener": {"host": "127.0.0.1", "port": 8080},
        "control": {"host": "127.0.0.1", "port": 9900},
        "balancer": {"strategy": "round_robin"},
        "backends": [
            {
                "name": "one",
                "host": "127.0.0.1",
                "port": 9001,
                "weight": 2,
            }
        ],
    }


class ConfigTests(unittest.TestCase):
    def test_valid_config(self):
        config = parse_config_data(valid_data())
        self.assertEqual(config.listener.port, 8080)
        self.assertEqual(config.backends[0].weight, 2)

    def test_invalid_port(self):
        data = valid_data()
        data["backends"][0]["port"] = 70000
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_invalid_weight(self):
        data = valid_data()
        data["backends"][0]["weight"] = 0
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_invalid_strategy(self):
        data = valid_data()
        data["balancer"]["strategy"] = "telepathy"
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_invalid_rule_rejects_config(self):
        data = valid_data()
        data["rules"] = {"source": 'if os.password == "x" then return "nope"'}
        with self.assertRaises(RuleSyntaxError):
            parse_config_data(data)


if __name__ == "__main__":
    unittest.main()

