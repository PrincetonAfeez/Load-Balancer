"""Extended configuration parser tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from load_balancer.config_parser import (
    MAX_BACKEND_WEIGHT,
    load_config,
    parse_config_data,
)
from load_balancer.errors import ConfigError


def valid_data(**extra):
    data = {
        "listener": {"host": "127.0.0.1", "port": 8080},
        "control": {"host": "127.0.0.1", "port": 9900},
        "backends": [{"name": "one", "host": "127.0.0.1", "port": 9001}],
    }
    data.update(extra)
    return data


class ConfigExtendedTests(unittest.TestCase):
    def test_unknown_section_and_table_errors(self):
        data = valid_data()
        data["mystery"] = {}
        with self.assertRaises(ConfigError):
            parse_config_data(data)
        data = valid_data()
        data["listener"] = []
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_duplicate_backend_name_and_address(self):
        data = valid_data()
        data["backends"] = [
            {"name": "a", "host": "127.0.0.1", "port": 9001},
            {"name": "b", "host": "127.0.0.1", "port": 9001},
        ]
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_listener_control_same_address(self):
        data = valid_data()
        data["control"] = {"host": "127.0.0.1", "port": 8080}
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_backend_collides_with_listener(self):
        data = valid_data()
        data["backends"] = [{"name": "bad", "host": "127.0.0.1", "port": 8080}]
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_health_timeout_exceeds_interval(self):
        data = valid_data()
        data["health"] = {
            "interval_seconds": 1,
            "timeout_seconds": 2,
        }
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_max_connections_negative(self):
        data = valid_data()
        data["balancer"] = {"max_connections": -1}
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_batch_size_exceeds_queue(self):
        data = valid_data()
        data["metrics"] = {"queue_size": 10, "batch_size": 20}
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_rule_instruction_limit(self):
        data = valid_data()
        data["rules"] = {"source": 'return "default"', "max_instructions": 1}
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_backend_weight_max(self):
        data = valid_data()
        data["backends"][0]["weight"] = MAX_BACKEND_WEIGHT + 1
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_backend_tags_must_be_strings(self):
        data = valid_data()
        data["backends"][0]["tags"] = [1]
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_unknown_balancer_key(self):
        data = valid_data()
        data["balancer"] = {"strategy": "round_robin", "unknown_key": 1}
        with self.assertRaises(ConfigError):
            parse_config_data(data)

    def test_load_config_file_missing(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/path.toml")

    def test_load_config_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.toml"
            path.write_text(
                """
                [listener]
                host = "127.0.0.1"
                port = 8081
                [control]
                host = "127.0.0.1"
                port = 9901
                [[backends]]
                name = "echo"
                host = "127.0.0.1"
                port = 9002
                """,
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.listener.port, 8081)

    def test_app_config_to_json(self):
        config = parse_config_data(valid_data())
        self.assertIn("listener", config.to_json())


if __name__ == "__main__":
    unittest.main()
