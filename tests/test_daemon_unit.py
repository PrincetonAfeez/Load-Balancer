"""Daemon PID file and unit tests."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from load_balancer.config_parser import AppConfig, BackendConfig, ListenerConfig
from load_balancer.daemon import LoadBalancerDaemon, PIDFile
from load_balancer.metrics import Metrics
from load_balancer.pool import BackendPool
from load_balancer.proxy import TCPProxy


class PIDFileTests(unittest.TestCase):
    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pid"
            pid = PIDFile(path)
            pid.acquire()
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), str(os.getpid()))
            pid.release()
            self.assertFalse(path.exists())

    def test_stale_pid_file_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pid"
            path.write_text("99999999", encoding="utf-8")
            pid = PIDFile(path)
            pid.acquire()
            self.assertEqual(path.read_text(encoding="utf-8"), str(os.getpid()))
            pid.release()

    @patch.object(PIDFile, "_is_alive", return_value=True)
    def test_running_pid_blocks_acquire(self, _mock_alive):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pid"
            path.write_text("12345", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                PIDFile(path).acquire()

    @patch.object(PIDFile, "_is_alive", return_value=False)
    def test_permission_error_treated_as_stale(self, mock_alive):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pid"
            path.write_text("1", encoding="utf-8")
            PIDFile(path).acquire()
            mock_alive.assert_called()


class DaemonHandleCommandTests(unittest.IsolatedAsyncioTestCase):
    def _minimal_daemon(self) -> LoadBalancerDaemon:
        config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 18080),
            backends=(
                BackendConfig("one", "127.0.0.1", 9001),
                BackendConfig("two", "127.0.0.1", 9002),
            ),
        )
        daemon = LoadBalancerDaemon("unused.toml")
        daemon.config = config
        daemon.pool = BackendPool.from_configs(config.backends)
        daemon.metrics = Metrics(100)
        daemon.proxy = TCPProxy(config, daemon.pool, daemon.metrics)
        return daemon

    async def test_status_and_strategy_get(self):
        daemon = self._minimal_daemon()
        status = await daemon.handle_command("status", {})
        self.assertEqual(status["strategy"], "round_robin")
        strategy = await daemon.handle_command("strategy.get", {})
        self.assertEqual(strategy["configured_strategy"], "round_robin")

    async def test_backends_list_and_mutations(self):
        daemon = self._minimal_daemon()
        listed = await daemon.handle_command("backends.list", {})
        self.assertEqual(len(listed["backends"]), 2)
        backend_id = listed["backends"][0]["id"]
        disabled = await daemon.handle_command(
            "backends.disable", {"backend_id": backend_id}
        )
        self.assertEqual(disabled["backend"]["state"], "disabled")
        enabled = await daemon.handle_command(
            "backends.enable", {"backend_id": backend_id}
        )
        self.assertEqual(enabled["backend"]["state"], "healthy")

    async def test_backends_add_and_remove(self):
        daemon = self._minimal_daemon()
        added = await daemon.handle_command(
            "backends.add",
            {"name": "three", "host": "127.0.0.1", "port": 9003, "weight": 1},
        )
        backend_id = added["backend"]["id"]
        removed = await daemon.handle_command(
            "backends.remove", {"backend_id": backend_id}
        )
        self.assertEqual(removed["backend"]["id"], backend_id)

    async def test_strategy_set_and_unknown_backend(self):
        daemon = self._minimal_daemon()
        changed = await daemon.handle_command(
            "strategy.set", {"name": "least_connections"}
        )
        self.assertEqual(changed["strategy"], "least_connections")
        with self.assertRaises(ValueError):
            await daemon.handle_command(
                "backends.drain", {"backend_id": "missing"}
            )

    async def test_unsupported_command(self):
        daemon = self._minimal_daemon()
        with self.assertRaises(ValueError):
            await daemon.handle_command("bogus", {})


if __name__ == "__main__":
    unittest.main()
