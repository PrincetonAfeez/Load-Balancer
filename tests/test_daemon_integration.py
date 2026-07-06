""" Test the daemon integration """

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
import tempfile
import textwrap
import unittest

from load_balancer.control import ControlClient
from load_balancer.crypto import generate_secret
from load_balancer.daemon import LoadBalancerDaemon
from load_balancer.errors import AdminProtocolError


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DaemonIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_reload_relay_and_shutdown(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        backend_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        backend_port = int(backend_server.sockets[0].getsockname()[1])
        listener_port = unused_port()
        control_port = unused_port()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "test.toml"
            secret_path = root / "secret.txt"
            database_path = root / "metrics.db"
            pid_path = root / "daemon.pid"
            secret = generate_secret()
            secret_path.write_text(secret, encoding="utf-8")

            def config_text(*, weight: int = 1, rule: str = 'return "default"'):
                return textwrap.dedent(
                    f"""
                    [listener]
                    host = "127.0.0.1"
                    port = {listener_port}

                    [balancer]
                    strategy = "round_robin"
                    drain_timeout_seconds = 1
                    pid_file = "{pid_path.as_posix()}"

                    [health]
                    interval_seconds = 0.1
                    timeout_seconds = 0.1
                    failures_to_unhealthy = 2
                    successes_to_healthy = 1

                    [timeouts]
                    connect_seconds = 0.5
                    idle_seconds = 2

                    [metrics]
                    database_path = "{database_path.as_posix()}"
                    flush_interval_seconds = 0.05
                    snapshot_interval_seconds = 0.1
                    queue_size = 1000
                    batch_size = 20

                    [control]
                    host = "127.0.0.1"
                    port = {control_port}
                    max_clock_skew_seconds = 30

                    [crypto]
                    secret_file = "{secret_path.as_posix()}"

                    [rules]
                    source = '{rule}'

                    [[backends]]
                    name = "echo"
                    host = "127.0.0.1"
                    port = {backend_port}
                    weight = {weight}
                    """
                )

            config_path.write_text(config_text(), encoding="utf-8")
            daemon = LoadBalancerDaemon(config_path)
            daemon._install_signal_handlers = lambda: None
            daemon_task = asyncio.create_task(daemon.run())
            client = ControlClient("127.0.0.1", control_port, secret, timeout=1)

            for _ in range(100):
                try:
                    status = await client.command("status")
                    break
                except (OSError, TimeoutError):
                    await asyncio.sleep(0.02)
            else:
                self.fail("daemon did not start")

            self.assertEqual(status["strategy"], "round_robin")

            def snapshot_count() -> int:
                with closing(sqlite3.connect(database_path)) as connection:
                    row = connection.execute(
                        "SELECT COUNT(*) FROM config_snapshots"
                    ).fetchone()
                    return int(row[0])

            self.assertEqual(snapshot_count(), 1)
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", listener_port
            )
            writer.write(b"daemon echo")
            await writer.drain()
            self.assertEqual(await reader.readexactly(11), b"daemon echo")
            writer.close()
            await writer.wait_closed()

            changed = await client.command(
                "strategy.set", {"name": "least_connections"}
            )
            self.assertEqual(changed["strategy"], "least_connections")

            config_path.write_text(
                config_text(rule='if client.ip == then return "bad"'),
                encoding="utf-8",
            )
            with self.assertRaises(AdminProtocolError):
                await client.command("reload")
            status = await client.command("status")
            self.assertEqual(status["strategy"], "least_connections")
            self.assertEqual(snapshot_count(), 1)

            config_path.write_text(config_text(weight=3), encoding="utf-8")
            reloaded = await client.command("reload")
            self.assertTrue(reloaded["reloaded"])
            status = await client.command("status")
            self.assertEqual(status["backends"][0]["weight"], 3)
            self.assertEqual(status["strategy"], "round_robin")
            self.assertEqual(snapshot_count(), 2)

            # Adding a backend at an address already in use is rejected (#10).
            with self.assertRaises(AdminProtocolError):
                await client.command(
                    "backends.add",
                    {"name": "dupe", "host": "127.0.0.1", "port": backend_port},
                )

            # Changing a restart-only metrics setting is rejected on reload (#9).
            config_path.write_text(
                config_text(weight=3).replace(
                    database_path.as_posix(), (root / "other.db").as_posix()
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AdminProtocolError):
                await client.command("reload")
            config_path.write_text(config_text(weight=3), encoding="utf-8")
            status = await client.command("status")
            self.assertEqual(status["connections_total"], 0)

            # Reload with an out-of-range backend port is rejected and the prior
            # good config keeps running (transactional rollback).
            config_path.write_text(
                config_text(weight=3).replace(f"port = {backend_port}", "port = 70000"),
                encoding="utf-8",
            )
            with self.assertRaises(AdminProtocolError):
                await client.command("reload")
            status = await client.command("status")
            self.assertEqual(status["backends"][0]["weight"], 3)
            config_path.write_text(config_text(weight=3), encoding="utf-8")

            # Reload moving a backend onto the listener address is rejected.
            config_path.write_text(
                config_text(weight=3).replace(
                    f"port = {backend_port}", f"port = {listener_port}"
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AdminProtocolError):
                await client.command("reload")
            config_path.write_text(config_text(weight=3), encoding="utf-8")

            await client.command("stop")
            await asyncio.wait_for(daemon_task, timeout=3)
            self.assertFalse(pid_path.exists())
            self.assertTrue(database_path.exists())

        backend_server.close()
        await backend_server.wait_closed()

    async def test_reload_removed_backend_force_closes_connections(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        backend_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        backend_port = int(backend_server.sockets[0].getsockname()[1])
        other_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        other_port = int(other_server.sockets[0].getsockname()[1])
        listener_port = unused_port()
        control_port = unused_port()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "test.toml"
            secret_path = root / "secret.txt"
            database_path = root / "metrics.db"
            pid_path = root / "daemon.pid"
            secret = generate_secret()
            secret_path.write_text(secret, encoding="utf-8")

            def config_text(*, backends: str) -> str:
                return textwrap.dedent(
                    f"""
                    [listener]
                    host = "127.0.0.1"
                    port = {listener_port}

                    [balancer]
                    strategy = "round_robin"
                    drain_timeout_seconds = 1
                    pid_file = "{pid_path.as_posix()}"

                    [health]
                    interval_seconds = 0.1
                    timeout_seconds = 0.1
                    failures_to_unhealthy = 2
                    successes_to_healthy = 1

                    [timeouts]
                    connect_seconds = 0.5
                    idle_seconds = 2

                    [metrics]
                    database_path = "{database_path.as_posix()}"
                    flush_interval_seconds = 0.05
                    snapshot_interval_seconds = 0.1
                    queue_size = 1000
                    batch_size = 20

                    [control]
                    host = "127.0.0.1"
                    port = {control_port}
                    max_clock_skew_seconds = 30

                    [crypto]
                    secret_file = "{secret_path.as_posix()}"

                    [rules]
                    source = 'return "default"'

                    {backends}
                    """
                )

            echo_backend = textwrap.dedent(
                f"""
                [[backends]]
                name = "echo"
                host = "127.0.0.1"
                port = {backend_port}
                weight = 1
                """
            )
            other_backend = textwrap.dedent(
                f"""
                [[backends]]
                name = "other"
                host = "127.0.0.1"
                port = {other_port}
                weight = 1
                """
            )

            config_path.write_text(config_text(backends=echo_backend), encoding="utf-8")
            daemon = LoadBalancerDaemon(config_path)
            daemon._install_signal_handlers = lambda: None
            daemon_task = asyncio.create_task(daemon.run())
            client = ControlClient("127.0.0.1", control_port, secret, timeout=1)

            for _ in range(100):
                try:
                    await client.command("status")
                    break
                except (OSError, TimeoutError):
                    await asyncio.sleep(0.02)
            else:
                self.fail("daemon did not start")

            reader, writer = await asyncio.open_connection("127.0.0.1", listener_port)
            writer.write(b"hold-open")
            await writer.drain()
            self.assertEqual(await reader.readexactly(9), b"hold-open")

            config_path.write_text(
                config_text(backends=other_backend), encoding="utf-8"
            )
            reloaded = await client.command("reload")
            self.assertTrue(reloaded["reloaded"])
            self.assertIn("echo", reloaded["changes"]["draining"])

            await asyncio.sleep(1.5)
            self.assertEqual(await reader.read(100), b"")

            writer.close()
            await writer.wait_closed()

            await client.command("stop")
            await asyncio.wait_for(daemon_task, timeout=3)

        backend_server.close()
        await backend_server.wait_closed()
        other_server.close()
        await other_server.wait_closed()


if __name__ == "__main__":
    unittest.main()

