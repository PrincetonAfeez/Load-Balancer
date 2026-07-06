"""CLI parser and helper tests."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from load_balancer.cli import (
    _emit,
    _print_status,
    _print_strategy_get,
    build_parser,
    dispatch,
    main,
)


class CliTests(unittest.TestCase):
    def test_build_parser_subcommands(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "x.toml", "init-db"])
        self.assertEqual(args.command, "init-db")
        args = parser.parse_args(
            ["backends", "add", "n", "127.0.0.1", "9001", "--weight", "2"]
        )
        self.assertEqual(args.backend_command, "add")
        args = parser.parse_args(["strategy", "set", "round_robin"])
        self.assertEqual(args.name, "round_robin")

    def test_start_requires_mode(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["start"])

    def test_print_status_and_strategy_get(self):
        status = {
            "pid": 1,
            "listener": {"host": "127.0.0.1", "port": 8080},
            "strategy": "least_connections",
            "configured_strategy": "round_robin",
            "shutdown_in_progress": True,
            "rule_can_override_strategy": True,
            "max_connections": 100,
            "connection_slots_in_use": 2,
            "connections": [],
            "connections_total": 0,
            "metrics": {
                "uptime_seconds": 1.5,
                "active_connections": 0,
                "total_connections": 5,
                "bytes_client_to_backend": 0,
                "bytes_backend_to_client": 0,
                "dropped_events": 0,
                "dropped_critical_events": 0,
            },
            "backends": [],
        }
        out = io.StringIO()
        with redirect_stdout(out):
            _print_status(status)
        text = out.getvalue()
        self.assertIn("Shutdown", text)
        self.assertIn("Slots", text)
        out = io.StringIO()
        with redirect_stdout(out):
            _print_strategy_get(
                {
                    "strategy": "round_robin",
                    "configured_strategy": "weighted_round_robin",
                    "rule_can_override_strategy": True,
                    "rule_source": 'return "default"',
                }
            )
        self.assertIn("may override", out.getvalue())

    def test_main_help_exits_zero(self):
        with patch("sys.argv", ["load-balancer", "--help"]):
            with self.assertRaises(SystemExit) as exc:
                main()
            self.assertEqual(exc.exception.code, 0)

    def test_emit_json_and_backend_list(self):
        out = io.StringIO()
        with redirect_stdout(out):
            _emit({"ok": True}, as_json=True)
        self.assertIn('"ok": true', out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            _emit(
                {
                    "backends": [
                        {
                            "id": "b1",
                            "host": "127.0.0.1",
                            "port": 9001,
                            "state": "healthy",
                            "active": 0,
                            "total": 1,
                            "weight": 1,
                        }
                    ]
                },
                as_json=False,
            )
        self.assertIn("b1", out.getvalue())


class CliDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_init_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "lb.toml"
            db_path = root / "metrics.db"
            secret_path = root / "secret.txt"
            secret_path.write_text("x" * 32, encoding="utf-8")
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    [listener]
                    host = "127.0.0.1"
                    port = 8080

                    [control]
                    host = "127.0.0.1"
                    port = 9900

                    [metrics]
                    database_path = "{db_path.as_posix()}"

                    [crypto]
                    secret_file = "{secret_path.as_posix()}"

                    [[backends]]
                    name = "one"
                    host = "127.0.0.1"
                    port = 9001
                    """
                ),
                encoding="utf-8",
            )
            parser = build_parser()
            args = parser.parse_args(["--config", str(config_path), "init-db"])
            result = await dispatch(args)
            self.assertTrue(Path(result["initialized"]).exists())


if __name__ == "__main__":
    unittest.main()
