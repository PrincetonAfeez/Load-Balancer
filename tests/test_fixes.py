"""Regression tests for the review fixes."""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
import tempfile
import textwrap
import unittest

from load_balancer.config_parser import parse_config_data
import load_balancer.control as control_module
from load_balancer.control import ControlClient, ControlServer, read_frame
from load_balancer.crypto import generate_secret
from load_balancer.daemon import LoadBalancerDaemon
from load_balancer.demo_tools import send_messages
from load_balancer.errors import AdminProtocolError, ConfigError
from load_balancer.metrics import Metrics
from load_balancer.store import SQLiteStore


def valid_data(**extra):
    data = {
        "listener": {"host": "127.0.0.1", "port": 8080},
        "control": {"host": "127.0.0.1", "port": 9900},
        "backends": [{"name": "one", "host": "127.0.0.1", "port": 9001}],
    }
    data.update(extra)
    return data


class ConfigValidationFixes(unittest.TestCase):
    def test_unknown_top_level_section_rejected(self):
        # Fix #3: a misspelled section is no longer silently ignored.
        with self.assertRaises(ConfigError):
            parse_config_data(valid_data(helth={"interval_seconds": 1}))
        with self.assertRaises(ConfigError):
            parse_config_data(valid_data(balncer={"strategy": "round_robin"}))

    def test_rule_exceeding_instruction_limit_rejected_at_load(self):
        # Fix #1: an oversized rule is rejected during validation, not left to
        # fail at runtime on every connection.
        source = (
            "if " + " or ".join(['client.ip == "x"'] * 40) + ' then return "round_robin"'
        )
        with self.assertRaises(ConfigError):
            parse_config_data(
                valid_data(rules={"source": source, "max_instructions": 10})
            )
        # The same rule is fine with a sufficient limit.
        config = parse_config_data(
            valid_data(rules={"source": source, "max_instructions": 256})
        )
        self.assertEqual(config.rules.max_instructions, 256)


class MetricsFixes(unittest.TestCase):
    def test_critical_events_bypass_a_full_normal_queue(self):
        # Fix #4: critical events use a dedicated intake queue and are not
        # dropped just because high-volume connection events filled the normal one.
        metrics = Metrics(queue_size=2)
        for index in range(20):
            metrics.emit("connection_opened", {"index": index})
        self.assertGreater(metrics.dropped_events, 0)
        self.assertEqual(metrics.dropped_critical_events, 0)
        metrics.emit("process_shutdown_started", {"reason": "test"}, critical=True)
        self.assertEqual(metrics.dropped_critical_events, 0)
        self.assertEqual(metrics.critical_queue.qsize(), 1)

    def test_event_counters_are_recorded(self):
        # Fix #14: the previously-dead counter map is now populated.
        metrics = Metrics(100)
        metrics.emit("connection_opened", {})
        metrics.emit("connection_opened", {})
        metrics.emit("health_check", {})
        counters = metrics.snapshot()["counters"]
        self.assertEqual(counters["connection_opened"], 2)
        self.assertEqual(counters["health_check"], 1)


class OversizeResponseFixes(unittest.IsolatedAsyncioTestCase):
    async def test_oversize_response_returns_error_frame(self):
        # Fix #2: a response that does not fit in a frame produces a structured
        # error instead of silently dropping the reply.
        secret = generate_secret()

        async def handler(command, args):
            return {"blob": "x" * 4000}

        server = ControlServer(
            "127.0.0.1",
            0,
            secret,
            max_frame_bytes=512,
            max_clock_skew_seconds=30,
            handler=handler,
            metrics=Metrics(100),
        )
        await server.start()
        assert server.server is not None
        port = int(server.server.sockets[0].getsockname()[1])
        try:
            client = ControlClient(
                "127.0.0.1", port, secret, max_frame_bytes=512, timeout=2
            )
            with self.assertRaises(AdminProtocolError) as caught:
                await client.command("status")
            self.assertIn("max_frame_bytes", str(caught.exception))
        finally:
            await server.close()


class Round2ConfigFixes(unittest.TestCase):
    def test_excessive_backend_weight_rejected(self):
        # Fix #1: an unbounded weight would blow up the consistent-hash ring.
        with self.assertRaises(ConfigError):
            parse_config_data(
                valid_data(
                    backends=[
                        {
                            "name": "big",
                            "host": "127.0.0.1",
                            "port": 9001,
                            "weight": 10_000_000,
                        }
                    ]
                )
            )

    def test_health_timeout_exceeding_interval_rejected(self):
        # Fix #5: a timeout longer than the interval makes checks overlap.
        with self.assertRaises(ConfigError):
            parse_config_data(
                valid_data(health={"interval_seconds": 1, "timeout_seconds": 2})
            )
        # Equal values are allowed.
        parse_config_data(
            valid_data(health={"interval_seconds": 1, "timeout_seconds": 1})
        )


class LbClientFixes(unittest.IsolatedAsyncioTestCase):
    async def test_partial_response_does_not_crash(self):
        # Fix #2: a backend that echoes part of the payload then closes is
        # reported as partial instead of raising IncompleteReadError.
        async def partial(reader, writer):
            data = await reader.read(65536)
            if data:
                writer.write(data[:1])
                await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(partial, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        try:
            results = await send_messages("127.0.0.1", port, "hello", 1, timeout=2)
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0]["complete"])
            self.assertEqual(results[0]["response"], "h")
        finally:
            server.close()
            await server.wait_closed()


class ControlTimeoutFixes(unittest.IsolatedAsyncioTestCase):
    async def test_idle_admin_client_is_timed_out(self):
        # Fix #11: a client that connects but sends nothing is timed out and
        # answered with an error rather than holding the handler forever.
        secret = generate_secret()

        async def handler(command, args):
            return {}

        server = ControlServer(
            "127.0.0.1",
            0,
            secret,
            max_frame_bytes=65536,
            max_clock_skew_seconds=30,
            handler=handler,
            metrics=Metrics(100),
        )
        original = control_module.CLIENT_READ_TIMEOUT
        control_module.CLIENT_READ_TIMEOUT = 0.2
        await server.start()
        assert server.server is not None
        port = int(server.server.sockets[0].getsockname()[1])
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            response = await asyncio.wait_for(read_frame(reader), timeout=2)
            self.assertFalse(response["ok"])
            writer.close()
            await writer.wait_closed()
        finally:
            control_module.CLIENT_READ_TIMEOUT = original
            await server.close()


class LeastConnectionsConcurrencyFixes(unittest.IsolatedAsyncioTestCase):
    async def test_counts_stay_correct_under_concurrent_load(self):
        # Fix #7: least-connections counter correctness under concurrency.
        from load_balancer.config_parser import (
            AppConfig,
            BackendConfig,
            BalancerConfig,
            ControlConfig,
            ListenerConfig,
            TimeoutConfig,
        )
        from load_balancer.pool import BackendPool
        from load_balancer.proxy import TCPProxy

        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        servers = [await asyncio.start_server(echo, "127.0.0.1", 0) for _ in range(3)]
        ports = [int(s.sockets[0].getsockname()[1]) for s in servers]
        config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 0),
            control=ControlConfig("127.0.0.1", 0),
            balancer=BalancerConfig(
                strategy="least_connections", drain_timeout_seconds=1
            ),
            timeouts=TimeoutConfig(connect_seconds=1, idle_seconds=5),
            backends=tuple(
                BackendConfig(f"b{i}", "127.0.0.1", p)
                for i, p in enumerate(ports, start=1)
            ),
        )
        pool = BackendPool.from_configs(config.backends)
        metrics = Metrics(1000)
        proxy = TCPProxy(config, pool, metrics)
        await proxy.start()
        host, port = proxy.address
        writers: list[asyncio.StreamWriter] = []
        try:
            conns = await asyncio.gather(
                *(asyncio.open_connection(host, port) for _ in range(30))
            )
            writers = [writer for _, writer in conns]
            for _, writer in conns:
                writer.write(b"x")
            await asyncio.gather(*(writer.drain() for _, writer in conns))
            for _ in range(200):
                if metrics.active_connections == 30:
                    break
                await asyncio.sleep(0.01)
            # Counter correctness: per-backend counts sum to the global count.
            self.assertEqual(metrics.active_connections, 30)
            actives = [backend.active_connections for backend in pool.all()]
            self.assertEqual(sum(actives), 30)
            self.assertTrue(all(count >= 1 for count in actives))
        finally:
            for writer in writers:
                writer.close()
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers),
                return_exceptions=True,
            )
            await proxy.drain(1)
            for server in servers:
                server.close()
                await server.wait_closed()
        # Cleanup correctness: everything returns to zero.
        self.assertEqual(metrics.active_connections, 0)
        self.assertTrue(
            all(backend.active_connections == 0 for backend in pool.all())
        )


class Round3HardeningTests(unittest.TestCase):
    def test_oversized_frame_limit_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_data(
                valid_data(
                    control={
                        "host": "127.0.0.1",
                        "port": 9900,
                        "max_frame_bytes": 64 * 1024 * 1024,
                    }
                )
            )

    def test_batch_size_exceeding_queue_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_data(
                valid_data(metrics={"queue_size": 100, "batch_size": 1000})
            )

    def test_backend_colliding_with_listener_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_data(
                valid_data(
                    backends=[{"name": "loop", "host": "127.0.0.1", "port": 8080}]
                )
            )


class ConfigSnapshotRetentionTests(unittest.TestCase):
    def test_retention_bounds_config_history(self):
        from contextlib import closing
        from pathlib import Path
        import tempfile

        from load_balancer.config_parser import AppConfig, BackendConfig
        from load_balancer.store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "metrics.db")
            store.initialize()
            config = AppConfig(backends=(BackendConfig("b", "127.0.0.1", 9001),))
            keep = SQLiteStore.CONFIG_SNAPSHOT_RETENTION
            for _ in range(keep + 10):
                store.save_config_snapshot(config, "test", [])
            with closing(store.connect()) as connection:
                snaps = connection.execute(
                    "SELECT COUNT(*) FROM config_snapshots"
                ).fetchone()[0]
                rules = connection.execute(
                    "SELECT COUNT(*) FROM rule_versions"
                ).fetchone()[0]
                backends = connection.execute(
                    "SELECT COUNT(*) FROM backend_config"
                ).fetchone()[0]
        self.assertEqual(snaps, keep)
        self.assertEqual(rules, keep)
        self.assertEqual(backends, keep)  # one backend per retained snapshot


class SchemaMigrationTests(unittest.TestCase):
    def test_initialize_is_idempotent_and_stamps_version(self):
        from contextlib import closing
        from pathlib import Path
        import tempfile

        import load_balancer.store as store_mod
        from load_balancer.store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "m.db")
            store.initialize()
            store.initialize()  # re-init must not error
            with closing(store.connect()) as connection:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            self.assertEqual(int(version), store_mod.SCHEMA_VERSION)

    def test_registered_migration_runs_on_upgrade(self):
        from contextlib import closing
        from pathlib import Path
        import tempfile

        import load_balancer.store as store_mod
        from load_balancer.store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "m.db")
            store.initialize()  # baseline at current version
            base = store_mod.SCHEMA_VERSION
            saved_version, saved_migs = store_mod.SCHEMA_VERSION, store_mod.MIGRATIONS
            store_mod.SCHEMA_VERSION = base + 1
            store_mod.MIGRATIONS = {
                **saved_migs,
                base + 1: ("CREATE TABLE migrated_marker(x INTEGER)",),
            }
            try:
                store.initialize()  # should apply the new migration
                with closing(store.connect()) as connection:
                    version = int(
                        connection.execute(
                            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                        ).fetchone()[0]
                    )
                    marker = connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'migrated_marker'"
                    ).fetchone()
            finally:
                store_mod.SCHEMA_VERSION = saved_version
                store_mod.MIGRATIONS = saved_migs
        self.assertEqual(version, base + 1)
        self.assertIsNotNone(marker)


class ImplementationFixesTests(unittest.TestCase):
    def test_reload_preserves_admin_drain(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool, BackendState

        pool = BackendPool.from_configs([BackendConfig("one", "127.0.0.1", 9001)])
        pool.drain("one")
        pool.apply_configs([BackendConfig("one", "127.0.0.1", 9001)])
        self.assertEqual(pool.require("one").state, BackendState.DRAINING)

    def test_reload_reenable_resets_health_streaks(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool, BackendState

        pool = BackendPool.from_configs([BackendConfig("one", "127.0.0.1", 9001)])
        backend = pool.require("one")
        backend.consecutive_failures = 5
        pool.disable("one")
        pool.apply_configs([BackendConfig("one", "127.0.0.1", 9001)])
        self.assertEqual(backend.state, BackendState.HEALTHY)
        self.assertEqual(backend.consecutive_failures, 0)

    def test_reserved_address_rejected_for_runtime_add(self):
        from load_balancer.config_parser import (
            AppConfig,
            BackendConfig,
            ControlConfig,
            ListenerConfig,
        )
        from load_balancer.daemon import LoadBalancerDaemon

        config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 8080),
            control=ControlConfig("127.0.0.1", 9900),
            backends=(BackendConfig("b1", "127.0.0.1", 9001),),
        )
        daemon = LoadBalancerDaemon("unused.toml")
        daemon.config = config
        with self.assertRaises(ValueError):
            daemon._reject_reserved_address("127.0.0.1", 8080)
        with self.assertRaises(ValueError):
            daemon._reject_reserved_address("127.0.0.1", 9900)

    def test_start_requires_foreground_or_daemon(self):
        from load_balancer.cli import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["start"])

    def test_event_table_retention(self):
        from contextlib import closing
        from pathlib import Path
        import tempfile

        from load_balancer.metrics import MetricEvent
        import load_balancer.store as store_mod
        from load_balancer.store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "events.db")
            store.initialize()
            saved = store_mod.SQLiteStore.EVENT_TABLE_RETENTION
            store_mod.SQLiteStore.EVENT_TABLE_RETENTION = 5
            try:
                events = [
                    MetricEvent("health_check", 1.0, {"backend_id": "b"})
                    for _ in range(8)
                ]
                store.write_events(events)
                with closing(store.connect()) as connection:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM health_events"
                    ).fetchone()[0]
            finally:
                store_mod.SQLiteStore.EVENT_TABLE_RETENTION = saved
        self.assertEqual(count, 5)


class ImplementationFixesAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_connections_balance_least_connections(self):
        from load_balancer.config_parser import (
            AppConfig,
            BackendConfig,
            BalancerConfig,
            ControlConfig,
            ListenerConfig,
            TimeoutConfig,
        )
        from load_balancer.pool import BackendPool
        from load_balancer.strategies import ConnectionContext, LeastConnectionsStrategy

        async def hang(reader, writer):
            await reader.read()

        servers = [await asyncio.start_server(hang, "127.0.0.1", 0) for _ in range(2)]
        ports = [int(s.sockets[0].getsockname()[1]) for s in servers]
        config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 0),
            control=ControlConfig("127.0.0.1", 0),
            balancer=BalancerConfig(strategy="least_connections"),
            timeouts=TimeoutConfig(connect_seconds=1, idle_seconds=5),
            backends=tuple(
                BackendConfig(f"b{i}", "127.0.0.1", p) for i, p in enumerate(ports, 1)
            ),
        )
        pool = BackendPool.from_configs(config.backends)
        strategy = LeastConnectionsStrategy()
        context = ConnectionContext("127.0.0.1")
        first = strategy.select(pool, context)
        first.begin_connection_attempt()
        second = strategy.select(pool, context)
        self.assertNotEqual(first.id, second.id)
        first.end_connection_attempt()
        for server in servers:
            server.close()
            await server.wait_closed()


class Round2FixesTests(unittest.TestCase):
    def test_reload_rejects_address_change_with_active_connections(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool

        pool = BackendPool.from_configs([BackendConfig("one", "127.0.0.1", 9001)])
        backend = pool.require("one")
        backend.begin_connection_attempt()
        with self.assertRaises(ValueError):
            pool.apply_configs([BackendConfig("one", "127.0.0.1", 9002)])
        backend.end_connection_attempt()

    def test_prune_retired_waits_for_pending_connections(self):
        from load_balancer.pool import Backend, BackendPool

        backend = Backend("one", "127.0.0.1", 9001, retired=True)
        backend.pending_connections = 1
        pool = BackendPool([backend])
        self.assertEqual(pool.prune_retired(), [])
        backend.pending_connections = 0
        self.assertEqual(pool.prune_retired(), ["one"])

    def test_round_robin_retry_does_not_advance_index(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool
        from load_balancer.strategies import ConnectionContext, RoundRobinStrategy

        pool = BackendPool.from_configs(
            [
                BackendConfig("b1", "127.0.0.1", 9001),
                BackendConfig("b2", "127.0.0.1", 9002),
            ]
        )
        strategy = RoundRobinStrategy()
        context = ConnectionContext("127.0.0.1")
        first = strategy.select(pool, context, advance=True)
        self.assertEqual(first.id, "b1")
        context.excluded_backend_ids.add("b1")
        retry = strategy.select(pool, context, advance=False)
        self.assertEqual(retry.id, "b2")


class Round2RuleAndCryptoTests(unittest.TestCase):
    def test_contains_on_tag_list(self):
        from load_balancer.rule_dsl import compile_rule
        from load_balancer.vm import VirtualMachine

        program = compile_rule(
            'if backend.tags contains "stable" then return "round_robin"'
        )
        result = VirtualMachine().execute(
            program, {"backend": {"tags": ["stable", "canary"]}}
        )
        self.assertEqual(result, "round_robin")

    def test_signed_response_verification(self):
        from load_balancer.crypto import sign_response, verify_response

        payload = {
            "version": 1,
            "timestamp": int(__import__("time").time()),
            "nonce": "resp-nonce",
            "ok": True,
            "data": {"status": "ok"},
        }
        payload["signature"] = sign_response(payload, "test-secret-long-enough-for-hmac")
        verify_response(payload, "test-secret-long-enough-for-hmac", 30)


class Round3FixesTests(unittest.IsolatedAsyncioTestCase):
    def test_pool_add_rejects_duplicate_address(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool

        pool = BackendPool.from_configs([BackendConfig("first", "127.0.0.1", 9001)])
        with self.assertRaises(ValueError):
            pool.add(BackendConfig("second", "127.0.0.1", 9001))

    def test_apply_configs_rejects_duplicate_address(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool

        pool = BackendPool.from_configs([BackendConfig("first", "127.0.0.1", 9001)])
        with self.assertRaises(ValueError):
            pool.apply_configs(
                [
                    BackendConfig("first", "127.0.0.1", 9001),
                    BackendConfig("second", "127.0.0.1", 9001),
                ]
            )

    def test_weighted_round_robin_retry_preserves_state(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool
        from load_balancer.strategies import ConnectionContext, SmoothWeightedRoundRobinStrategy

        pool = BackendPool.from_configs(
            [
                BackendConfig("b1", "127.0.0.1", 9001, weight=2),
                BackendConfig("b2", "127.0.0.1", 9002, weight=1),
            ]
        )
        strategy = SmoothWeightedRoundRobinStrategy()
        context = ConnectionContext("127.0.0.1")
        strategy.select(pool, context, advance=True)
        state_after_advance = dict(strategy._current)
        strategy.select(pool, context, advance=False)
        self.assertEqual(dict(strategy._current), state_after_advance)

    def test_response_nonce_replay_rejected(self):
        from load_balancer.crypto import (
            ReplayError,
            ResponseNonceCache,
            sign_response,
            verify_response,
        )

        payload = {
            "version": 1,
            "timestamp": int(__import__("time").time()),
            "nonce": "round3-response-nonce",
            "ok": True,
            "data": {"status": "ok"},
        }
        secret = "test-secret-long-enough-for-hmac"
        payload["signature"] = sign_response(payload, secret)
        cache = ResponseNonceCache(60)
        verify_response(payload, secret, 30, cache)
        with self.assertRaises(ReplayError):
            verify_response(payload, secret, 30, cache)

    def test_rule_can_override_strategy(self):
        from load_balancer.rule_dsl import rule_can_override_strategy

        self.assertFalse(rule_can_override_strategy('return "default"'))
        self.assertTrue(rule_can_override_strategy('return "round_robin"'))
        self.assertTrue(
            rule_can_override_strategy(
                'if client.ip == "10.0.0.1" then return "least_connections"'
            )
        )

    def test_foreign_keys_enforced(self):
        from load_balancer.store import SQLiteStore

        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "metrics.db"
            store = SQLiteStore(database_path)
            store.initialize()
            with closing(store.connect()) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO backend_config(
                            snapshot_id, backend_id, host, port, weight, enabled, tags_json
                        ) VALUES(99999, 'ghost', '127.0.0.1', 1, 1, 1, '[]')
                        """
                    )
                    connection.commit()

    def test_apply_configs_rejects_address_update_collision(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool

        pool = BackendPool.from_configs(
            [
                BackendConfig("a", "127.0.0.1", 9001),
                BackendConfig("b", "127.0.0.1", 9002),
            ]
        )
        with self.assertRaises(ValueError):
            pool.apply_configs(
                [
                    BackendConfig("a", "127.0.0.1", 9002),
                    BackendConfig("b", "127.0.0.1", 9002),
                ]
            )

    def test_apply_configs_rejects_collision_with_retired_backend(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool

        pool = BackendPool.from_configs(
            [
                BackendConfig("a", "127.0.0.1", 9001),
                BackendConfig("b", "127.0.0.1", 9002),
            ]
        )
        retiring = pool.require("b")
        retiring.begin_connection_attempt()
        pool.drain("b", retired=True)
        with self.assertRaises(ValueError):
            pool.apply_configs([BackendConfig("a", "127.0.0.1", 9002)])
        retiring.end_connection_attempt()

    def test_apply_configs_rejects_reserved_address(self):
        from load_balancer.config_parser import BackendConfig
        from load_balancer.pool import BackendPool

        pool = BackendPool.from_configs([BackendConfig("a", "127.0.0.1", 9001)])
        with self.assertRaises(ValueError):
            pool.apply_configs(
                [BackendConfig("b", "127.0.0.1", 8080)],
                reserved_addresses=frozenset({("127.0.0.1", 8080)}),
            )


class Round4ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutating_commands_rejected_during_shutdown(self):
        from load_balancer.config_parser import AppConfig, BackendConfig, ListenerConfig
        from load_balancer.daemon import LoadBalancerDaemon
        from load_balancer.metrics import Metrics
        from load_balancer.pool import BackendPool
        from load_balancer.proxy import TCPProxy

        config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 18080),
            backends=(BackendConfig("one", "127.0.0.1", 9001),),
        )
        daemon = LoadBalancerDaemon("unused.toml")
        daemon.config = config
        daemon.pool = BackendPool.from_configs(config.backends)
        daemon.metrics = Metrics(100)
        daemon.proxy = TCPProxy(config, daemon.pool, daemon.metrics)
        daemon.request_shutdown("test")

        with self.assertRaises(ValueError):
            await daemon.handle_command("reload", {})
        with self.assertRaises(ValueError):
            await daemon.handle_command("strategy.set", {"name": "least_connections"})
        status = await daemon.handle_command("status", {})
        self.assertTrue(status["shutdown_in_progress"])

    async def test_max_connections_rejects_overflow(self):
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

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            listener_port = int(sock.getsockname()[1])
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            control_port = int(sock.getsockname()[1])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "test.toml"
            secret_path = root / "secret.txt"
            database_path = root / "metrics.db"
            pid_path = root / "daemon.pid"
            secret = generate_secret()
            secret_path.write_text(secret, encoding="utf-8")

            config_text = textwrap.dedent(
                f"""
                [listener]
                host = "127.0.0.1"
                port = {listener_port}

                [balancer]
                strategy = "round_robin"
                drain_timeout_seconds = 1
                pid_file = "{pid_path.as_posix()}"
                max_connections = 1

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

                [[backends]]
                name = "echo"
                host = "127.0.0.1"
                port = {backend_port}
                weight = 1
                """
            )
            config_path.write_text(config_text, encoding="utf-8")
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

            self.assertEqual(status["max_connections"], 1)
            hold_reader, hold_writer = await asyncio.open_connection(
                "127.0.0.1", listener_port
            )
            hold_writer.write(b"hold")
            await hold_writer.drain()

            overflow_reader, overflow_writer = await asyncio.open_connection(
                "127.0.0.1", listener_port
            )
            overflow_writer.close()
            await overflow_writer.wait_closed()
            await asyncio.sleep(0.2)

            store = SQLiteStore(database_path)
            history = store.routing_history(limit=10)
            rejected = [
                event
                for event in history
                if event["event"] == "connection_rejected"
                and event["data"].get("reason") == "max_connections"
            ]
            self.assertTrue(rejected)

            hold_writer.close()
            await hold_writer.wait_closed()
            await hold_reader.read()

            await client.command("stop")
            await asyncio.wait_for(daemon_task, timeout=3)

        backend_server.close()
        await backend_server.wait_closed()


class Round5FixesTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_aborts_if_shutdown_requested_after_parse(self):
        from unittest.mock import MagicMock, patch

        from load_balancer.config_parser import AppConfig, BackendConfig, ListenerConfig
        from load_balancer.daemon import LoadBalancerDaemon
        from load_balancer.metrics import Metrics
        from load_balancer.pool import BackendPool
        from load_balancer.proxy import TCPProxy

        config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 18080),
            backends=(BackendConfig("one", "127.0.0.1", 9001),),
        )
        daemon = LoadBalancerDaemon("unused.toml")
        daemon.config = config
        daemon.pool = BackendPool.from_configs(config.backends)
        daemon.metrics = Metrics(100)
        daemon.proxy = TCPProxy(config, daemon.pool, daemon.metrics)
        daemon.health = MagicMock()
        daemon.store = MagicMock()

        def load_and_shutdown(_path: str) -> AppConfig:
            daemon.request_shutdown("test")
            return config

        with patch("load_balancer.daemon.load_config", load_and_shutdown):
            with self.assertRaises(ValueError):
                await daemon.reload()
        daemon.store.save_config_snapshot.assert_not_called()

    async def test_graceful_shutdown_waits_for_reload_lock(self):
        from unittest.mock import AsyncMock, MagicMock

        from load_balancer.config_parser import AppConfig, BackendConfig, ListenerConfig
        from load_balancer.daemon import LoadBalancerDaemon
        from load_balancer.metrics import Metrics
        from load_balancer.pool import BackendPool
        from load_balancer.proxy import TCPProxy

        config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 18080),
            backends=(BackendConfig("one", "127.0.0.1", 9001),),
        )
        daemon = LoadBalancerDaemon("unused.toml")
        daemon.config = config
        daemon.pool = BackendPool.from_configs(config.backends)
        daemon.metrics = Metrics(100)
        daemon.proxy = TCPProxy(config, daemon.pool, daemon.metrics)
        daemon.health = MagicMock()
        daemon.writer = MagicMock()
        daemon.control = MagicMock()
        daemon.control.close = AsyncMock()
        daemon.proxy.drain = AsyncMock()
        daemon._background_tasks = []

        async def holding_reload() -> None:
            async with daemon._reload_lock:
                await asyncio.sleep(0.15)

        reload_task = asyncio.create_task(holding_reload())
        await asyncio.sleep(0.02)
        shutdown_task = asyncio.create_task(daemon._graceful_shutdown())
        await asyncio.sleep(0.05)
        self.assertFalse(shutdown_task.done())
        await reload_task
        await shutdown_task


if __name__ == "__main__":
    unittest.main()
