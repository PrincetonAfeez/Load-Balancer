"""Automated end-to-end capstone demonstration.

Run after `python -m pip install -e .`:
    python scripts/demo.py
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
import socket
import tempfile
import textwrap

from load_balancer.config_parser import BackendConfig
from load_balancer.control import ControlClient
from load_balancer.crypto import generate_secret
from load_balancer.daemon import LoadBalancerDaemon
from load_balancer.pool import BackendPool
from load_balancer.rule_dsl import compile_rule
from load_balancer.store import SQLiteStore
from load_balancer.strategies import ConnectionContext, ConsistentHashStrategy
from load_balancer.vm import VirtualMachine


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def tagged_backend(name: str, port: int) -> asyncio.AbstractServer:
    async def handle(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(name.encode() + b":" + data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_server(handle, "127.0.0.1", port)


async def request(port: int, message: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        payload = message.encode()
        writer.write(payload)
        await writer.drain()
        response = await asyncio.wait_for(
            reader.readuntil(b":" + payload), timeout=2
        )
        return response.decode()
    finally:
        writer.close()
        await writer.wait_closed()


async def wait_for_state(
    client: ControlClient, backend_id: str, state: str, timeout: float = 4
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = await client.command("status")
        backend = next(
            item for item in status["backends"] if item["id"] == backend_id
        )
        if backend["state"] == state:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"{backend_id} did not become {state}")


async def main() -> None:
    listener_port, control_port = free_port(), free_port()
    backend_ports = [free_port() for _ in range(3)]
    servers = [
        await tagged_backend(f"b{index}", port)
        for index, port in enumerate(backend_ports, start=1)
    ]

    with tempfile.TemporaryDirectory(prefix="load-balancer-demo-") as temporary:
        root = Path(temporary)
        config_path = root / "demo.toml"
        secret_path = root / "admin.secret"
        database_path = root / "demo.db"
        pid_path = root / "demo.pid"
        secret = generate_secret()
        secret_path.write_text(secret, encoding="utf-8")

        def write_config(weight: int = 3) -> None:
            backend_blocks = "\n".join(
                textwrap.dedent(
                    f"""
                    [[backends]]
                    name = "b{index}"
                    host = "127.0.0.1"
                    port = {port}
                    weight = {weight if index == 1 else 1}
                    """
                )
                for index, port in enumerate(backend_ports, start=1)
            )
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    [listener]
                    host = "127.0.0.1"
                    port = {listener_port}

                    [balancer]
                    strategy = "round_robin"
                    drain_timeout_seconds = 3
                    pid_file = "{pid_path.as_posix()}"
                    virtual_nodes_per_weight = 128

                    [health]
                    interval_seconds = 0.2
                    timeout_seconds = 0.1
                    failures_to_unhealthy = 2
                    successes_to_healthy = 2

                    [timeouts]
                    connect_seconds = 0.3
                    idle_seconds = 10

                    [metrics]
                    database_path = "{database_path.as_posix()}"
                    flush_interval_seconds = 0.1
                    snapshot_interval_seconds = 0.5
                    queue_size = 5000
                    batch_size = 100

                    [control]
                    host = "127.0.0.1"
                    port = {control_port}

                    [crypto]
                    secret_file = "{secret_path.as_posix()}"

                    [rules]
                    source = '''
                    if client.ip startswith "10." then return "consistent_hash"
                    else return "default"
                    '''

                    {backend_blocks}
                    """
                ),
                encoding="utf-8",
            )

        write_config()
        daemon = LoadBalancerDaemon(config_path)
        daemon_task = asyncio.create_task(daemon.run())
        client = ControlClient("127.0.0.1", control_port, secret)

        try:
            for _ in range(100):
                try:
                    await client.command("status")
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            print("1-3. Database, secret, three backends, and balancer started.")

            responses = [await request(listener_port, f"rr-{i}") for i in range(6)]
            print("4-5. Round robin:", [item.split(":")[0] for item in responses])

            await client.command(
                "strategy.set", {"name": "weighted_round_robin"}
            )
            responses = [await request(listener_port, f"w-{i}") for i in range(15)]
            print(
                "6. Weighted distribution:",
                dict(Counter(item.split(":")[0] for item in responses)),
            )

            await client.command("strategy.set", {"name": "least_connections"})
            held = [
                await asyncio.open_connection("127.0.0.1", listener_port)
                for _ in range(3)
            ]
            await asyncio.sleep(0.1)
            status = await client.command("status")
            print(
                "7. Least-connections active counts:",
                {b["id"]: b["active"] for b in status["backends"]},
            )
            for _, writer in held:
                writer.close()
                await writer.wait_closed()

            ring_pool = BackendPool.from_configs(
                BackendConfig(f"b{i}", "127.0.0.1", port)
                for i, port in enumerate(backend_ports, start=1)
            )
            ring = ConsistentHashStrategy(128)
            before = {
                key: ring.select(
                    ring_pool, ConnectionContext("127.0.0.1", sticky_key=key)
                ).id
                for key in (f"client-{i}" for i in range(1000))
            }
            ring_pool.add(BackendConfig("b4", "127.0.0.1", free_port()))
            after = {
                key: ring.select(
                    ring_pool, ConnectionContext("127.0.0.1", sticky_key=key)
                ).id
                for key in before
            }
            remap = sum(before[key] != after[key] for key in before) / len(before)
            print(f"8. Consistent-hash remapping after add: {remap:.1%}")

            servers[1].close()
            await servers[1].wait_closed()
            await wait_for_state(client, "b2", "unhealthy")
            print("9-11. b2 stopped, marked unhealthy, and excluded.")

            servers[1] = await tagged_backend("b2", backend_ports[1])
            await wait_for_state(client, "b2", "healthy")
            print("12-13. b2 restarted and returned to healthy.")

            await client.command("backends.drain", {"backend_id": "b3"})
            drained = [
                (await request(listener_port, f"d-{i}")).split(":")[0]
                for i in range(6)
            ]
            print("14. Traffic after draining b3:", drained)

            reader, writer = await asyncio.open_connection(
                "127.0.0.1", listener_port
            )
            writer.write(b"before")
            await writer.drain()
            first = await reader.readuntil(b":before")
            write_config(weight=4)
            await client.command("reload")
            writer.write(b"after")
            await writer.drain()
            second = await reader.readuntil(b":after")
            writer.close()
            await writer.wait_closed()
            print("15. Existing connection survived reload:", first, second)

            print("16. Every admin command above used HMAC + timestamp + nonce.")
            program = compile_rule(
                'if client.ip startswith "10." then return "consistent_hash" '
                'else return "round_robin"'
            )
            result = VirtualMachine().execute(
                program, {"client": {"ip": "10.0.0.7"}}
            )
            print("17. Compiled rule VM result:", result)

            await asyncio.sleep(0.6)
            summary = await asyncio.to_thread(
                SQLiteStore(database_path).metrics_summary
            )
            print(
                "18. SQLite event counts:",
                summary["connection_event_counts"],
            )
            await client.command("stop")
            await daemon_task
            print("19. Graceful shutdown complete.")
        finally:
            if not daemon_task.done():
                daemon.request_shutdown("demo-cleanup")
                await daemon_task
            for server in servers:
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
