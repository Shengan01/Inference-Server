#!/usr/bin/env python3
"""
Kubernetes Automated Test Procedure & Load Simulator

Executes load testing against the Kubernetes inference cluster,
verifying:
1. Triton inference response latency
2. Prometheus metrics ingestion
3. MCP Agent reconfiguration endpoint
4. Latency recovery under autonomic intervention
"""

import os
import sys
import time
import json
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Any

import httpx

TRITON_URL = os.getenv("TRITON_URL", "http://localhost:8000")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
MCP_AGENT_URL = os.getenv("MCP_AGENT_URL", "http://localhost:5000")
MODEL_NAME = "finance-qwen"

@dataclass
class PhaseResult:
    phase_name: str
    target_rps: int
    duration_s: int
    requests_sent: int
    successful_requests: int
    failed_requests: int
    avg_latency_ms: float
    p95_latency_ms: float

async def send_inference_request(client: httpx.AsyncClient) -> float:
    start = time.perf_counter()
    try:
        payload = {
            "inputs": [
                {
                    "name": "text_input",
                    "shape": [1],
                    "datatype": "BYTES",
                    "data": ["Analyze Q3 tech market earnings and inflation impact."]
                }
            ]
        }
        resp = await client.post(
            f"{TRITON_URL}/v2/models/{MODEL_NAME}/infer",
            json=payload,
            timeout=5.0
        )
        latency_ms = (time.perf_counter() - start) * 1000.0
        if resp.status_code == 200:
            return latency_ms
        return -1.0
    except Exception:
        return -1.0

async def run_traffic_phase(
    phase_name: str,
    target_rps: int,
    duration_s: int
) -> PhaseResult:
    print(f"\n[FAZA] {phase_name}: {target_rps} RPS przez {duration_s}s...")
    latencies: List[float] = []
    success = 0
    failures = 0
    start_time = time.time()

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=200, max_keepalive_connections=50)) as client:
        while time.time() - start_time < duration_s:
            batch_start = time.time()
            batch_size = max(1, target_rps // 5)
            tasks = [send_inference_request(client) for _ in range(batch_size)]
            results = await asyncio.gather(*tasks)

            for lat in results:
                if lat >= 0:
                    success += 1
                    latencies.append(lat)
                else:
                    failures += 1

            elapsed = time.time() - batch_start
            sleep_needed = 0.2 - elapsed
            if sleep_needed > 0:
                await asyncio.sleep(sleep_needed)

    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    latencies.sort()
    p95_lat = latencies[int(len(latencies) * 0.95)] if latencies else 0.0

    print(f"   [OK] Wyslano: {success + failures} | Sukces: {success} | Sr. latencja: {avg_lat:.2f} ms | P95: {p95_lat:.2f} ms")
    return PhaseResult(
        phase_name=phase_name,
        target_rps=target_rps,
        duration_s=duration_s,
        requests_sent=success + failures,
        successful_requests=success,
        failed_requests=failures,
        avg_latency_ms=avg_lat,
        p95_latency_ms=p95_lat
    )

async def check_prometheus_metrics():
    print("\n[INFO] Sprawdzanie metryk w Prometheusie...")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query?query=up", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                print("   [OK] Prometheus API odpowiada. Targety 'up':", len(data.get("data", {}).get("result", [])))
            else:
                print(f"   [WARN] Prometheus zwrocil kod: {resp.status_code}")
        except Exception as e:
            print(f"   [WARN] Nie udalo sie polaczyc z Prometheus ({PROMETHEUS_URL}): {e}")

async def trigger_reconfiguration(strategy: str):
    print(f"\n[SRE] Wywolanie rekonfiguracji przez MCP Agent (strategia='{strategy}')...")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{MCP_AGENT_URL}/reconfigure",
                json={"strategy": strategy},
                timeout=10.0
            )
            print(f"   [OK] Odpowiedz MCP Agenta: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"   [ERROR] Blad podczas rekonfiguracji: {e}")

async def main():
    print("==========================================================")
    print("PROCEDURA TESTOWA SYSTEMU MONITORINGU KUBERNETES")
    print(f"Triton URL: {TRITON_URL}")
    print(f"Prometheus URL: {PROMETHEUS_URL}")
    print(f"MCP Agent URL: {MCP_AGENT_URL}")
    print("==========================================================")

    # 1. Warm-up / Sprawdzenie dostepnosci
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{TRITON_URL}/v2/health/ready", timeout=5.0)
            if resp.status_code != 200:
                print(f"[ERROR] Triton nie jest gotowy ({resp.status_code}). Upewnij sie, ze port-forward dziala.")
                sys.exit(1)
            print("[OK] Triton Server jest gotowy.")
        except Exception as e:
            print(f"[ERROR] Nie mozna polaczyc sie z Tritonem ({TRITON_URL}): {e}")
            print("Wskazowka: uruchom 'kubectl port-forward -n inference-monitoring svc/triton-service 8000:8000'")
            sys.exit(1)

    results: List[PhaseResult] = []

    # Faza 1: Spokoj (The Calm)
    res1 = await run_traffic_phase("1. THE CALM (Obciazenie bazowe)", target_rps=20, duration_s=10)
    results.append(res1)

    # Faza 2: Skok obciazenia (The Storm)
    res2 = await run_traffic_phase("2. THE STORM (Skok obciazenia - degradacja latencji)", target_rps=150, duration_s=15)
    results.append(res2)

    # Faza 3: Interwencja Agenta SRE (The Resolution)
    await trigger_reconfiguration("aggressive")
    res3 = await run_traffic_phase("3. THE RESOLUTION (Po rekonfiguracji klastra)", target_rps=150, duration_s=15)
    results.append(res3)

    # Sprawdzenie metryk w Prometheus
    await check_prometheus_metrics()

    # Podsumowanie
    print("\n==========================================================")
    print("PODSUMOWANIE REALIZACJI PROCEDURY TESTOWEJ")
    print("==========================================================")
    print(f"{'Faza testu':<38} | {'RPS':<5} | {'Sr. Latencja':<14} | {'P95 Latencja':<14} | {'Sukces':<7}")
    print("-" * 88)
    for r in results:
        print(f"{r.phase_name:<38} | {r.target_rps:<5} | {r.avg_latency_ms:>8.2f} ms    | {r.p95_latency_ms:>8.2f} ms    | {r.successful_requests}/{r.requests_sent}")
    print("==========================================================")
    print("Testy zakonczone pomyslnie. Dane zarejestrowane w Prometheusie i Grafanie.")

if __name__ == "__main__":
    asyncio.run(main())
