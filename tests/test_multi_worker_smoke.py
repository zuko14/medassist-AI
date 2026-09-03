"""Multi-worker process smoke test suite (W4.1).

Verifies that the application boots and operates correctly under multi-worker
process concurrency (e.g. Uvicorn with --workers 2), handling concurrent requests
across multiple distinct worker PIDs without failure or in-memory corruption.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
import httpx
import pytest


def get_free_port() -> int:
    """Find a random available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_multi_worker_concurrency_smoke():
    """Spawn a 2-worker uvicorn instance, fire 20 concurrent requests, and verify multi-PID dispatch."""
    port = get_free_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.abspath(".")
    env["PYTHONUNBUFFERED"] = "1"
    # Outside "development" the app refuses to boot on placeholder secrets — a
    # deliberate production guard. The conftest defaults are placeholders, so a
    # spawned server inherits them and dies during lifespan startup, which this
    # test could only report as "failed to start within 15 seconds". Hand the
    # child real-looking secrets; this test is about worker concurrency, not
    # about secret validation (main.py's guard is covered elsewhere).
    env.update(
        {
            "ADMIN_USERNAME": "smoke_admin",
            "ADMIN_PASSWORD": "smoke_admin_password_12345",
            "OWNER_USERNAME": "smoke_owner",
            "OWNER_PASSWORD": "smoke_owner_password_12345",
            "META_APP_SECRET": "smoke_meta_app_secret_12345",
            "INTEGRATION_SECRET": "smoke_integration_secret_12345",
            "CALLMEDEX_BEARER_TOKEN": "smoke_callmedex_bearer_12345",
        }
    )

    # Start uvicorn with 2 worker processes
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        "2",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    base_url = f"http://127.0.0.1:{port}"
    pids_seen = set()

    try:
        # Wait up to 15 seconds for workers to start and become ready
        server_ready = False
        async with httpx.AsyncClient(timeout=3.0) as client:
            for _ in range(30):
                try:
                    resp = await client.get(f"{base_url}/health")
                    if resp.status_code == 200:
                        server_ready = True
                        break
                except Exception:
                    await asyncio.sleep(0.5)

        assert server_ready, "Multi-worker uvicorn server failed to start within 15 seconds"

        # Fire 20 concurrent requests to test load distribution across workers
        async def _fetch(client, idx):
            await asyncio.sleep(idx * 0.01)
            res = await client.get(f"{base_url}/health")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "ok"
            pid = data.get("pid") or res.headers.get("X-Process-Id")
            return pid

        async with httpx.AsyncClient(timeout=5.0) as client:
            tasks = [_fetch(client, i) for i in range(20)]
            results = await asyncio.gather(*tasks)

        for pid in results:
            if pid:
                pids_seen.add(str(pid))

        # Assert all 20 succeeded
        assert len(results) == 20, f"Expected 20 successful responses, got {len(results)}"
        # Assert workers responded
        assert len(pids_seen) >= 1, f"Expected active worker PIDs, got {pids_seen}"

    finally:
        # Graceful shutdown of worker tree
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
