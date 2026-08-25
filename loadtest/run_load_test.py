"""Standalone Production Load & Capacity Runner (W3.1, W3.2).

Executes realistic concurrency ramps against the application, calculates p50, p95, p99
latencies, checks for zero unhandled exceptions, and outputs a formatted capacity report.

Usage:
    python loadtest/run_load_test.py [--concurrency 50] [--requests 500] [--base-url http://localhost:8000]
"""

import argparse
import asyncio
import time
import statistics
import httpx
from typing import List, Dict, Any


async def simulate_webhook_request(client: httpx.AsyncClient, base_url: str, req_id: int) -> Dict[str, Any]:
    wamid = f"wamid.LOAD_{int(time.time()*1000)}_{req_id}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "100000000000000",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550234567",
                                "phone_number_id": "100000000000000",
                            },
                            "contacts": [{"profile": {"name": f"User {req_id}"}, "wa_id": f"91980000{req_id:04d}"}],
                            "messages": [
                                {
                                    "from": f"91980000{req_id:04d}",
                                    "id": wamid,
                                    "timestamp": str(int(time.time())),
                                    "text": {"body": "Book appointment"},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }

    t0 = time.perf_counter()
    try:
        res = await client.post(f"{base_url}/webhook", json=payload, timeout=10.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "status_code": res.status_code,
            "latency_ms": elapsed_ms,
            "success": res.status_code in (200, 202),
            "error": None if res.status_code in (200, 202) else res.text[:100],
        }
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "status_code": 0,
            "latency_ms": elapsed_ms,
            "success": False,
            "error": str(e),
        }


async def run_scenario(scenario_name: str, base_url: str, total_requests: int, concurrency: int):
    print(f"\n[{scenario_name}] Starting ramp: {total_requests} requests with concurrency={concurrency}...")
    sem = asyncio.Semaphore(concurrency)
    results: List[Dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        async def worker(idx: int):
            async with sem:
                res = await simulate_webhook_request(client, base_url, idx)
                results.append(res)

        t_start = time.perf_counter()
        tasks = [worker(i) for i in range(total_requests)]
        await asyncio.gather(*tasks)
        total_duration = time.perf_counter() - t_start

    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    latencies = [r["latency_ms"] for r in results]

    p50 = statistics.median(latencies) if latencies else 0
    p95 = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies or [0])
    p99 = statistics.quantiles(latencies, n=100)[98] if len(latencies) >= 100 else max(latencies or [0])
    throughput = len(results) / total_duration if total_duration > 0 else 0

    print(f"--- Results for {scenario_name} ---")
    print(f"Total Requests : {len(results)}")
    print(f"Successful     : {len(successful)} ({(len(successful)/len(results))*100:.1f}%)")
    print(f"Failed         : {len(failed)}")
    print(f"Duration       : {total_duration:.2f}s ({throughput:.1f} req/sec)")
    print(f"Latency p50    : {p50:.2f} ms")
    print(f"Latency p95    : {p95:.2f} ms")
    print(f"Latency p99    : {p99:.2f} ms")
    return {
        "scenario": scenario_name,
        "total": len(results),
        "success": len(successful),
        "failed": len(failed),
        "throughput_rps": throughput,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
    }


def main():
    parser = argparse.ArgumentParser(description="Kriya AI Capacity Load Runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base application URL")
    parser.add_argument("--requests", type=int, default=100, help="Total requests per scenario")
    parser.add_argument("--concurrency", type=int, default=20, help="Max concurrent connections")
    args = parser.parse_args()

    print("═══════════════════════════════════════════════════════════════════")
    print(" KRIYA AI REAL PRODUCTION CAPACITY & BENCHMARK RUNNER (W3.1-W3.3)  ")
    print(f" Target: {args.base_url} | Requests: {args.requests} | Concurrency: {args.concurrency}")
    print("═══════════════════════════════════════════════════════════════════")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            run_scenario("Webhook Ingest Ramp", args.base_url, args.requests, args.concurrency)
        )
    finally:
        loop.close()


if __name__ == "__main__":
    main()
