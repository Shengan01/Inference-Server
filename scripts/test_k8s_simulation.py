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
from typing import List, Dict, Any, Tuple

import httpx

TRITON_URL = os.getenv("TRITON_URL", "http://localhost:8000")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
MCP_AGENT_URL = os.getenv("MCP_AGENT_URL", "http://localhost:5000")
MODEL_NAME = "finance-qwen"
# Size of the per-second burst in the storm / resolution phases. It has to
# comfortably exceed the mock's default batch capacity (max_num_seqs=32) even
# when the load generator cannot fire every request at the same instant.
STORM_RPS = int(os.getenv("STORM_RPS", "400"))

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
    server_avg_latency_ms: float = -1.0

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

async def prom_scalar(client: httpx.AsyncClient, query: str) -> float:
    resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=5.0)
    result = resp.json().get("data", {}).get("result", [])
    return float(result[0]["value"][1]) if result else 0.0

async def triton_counters() -> Tuple[float, float]:
    """Cumulative server-side counters (sum of request duration in us, request count).

    Prometheus scrapes every second, so wait a little before reading to make
    sure the last requests of the previous phase are included.
    """
    await asyncio.sleep(3.0)
    async with httpx.AsyncClient() as client:
        try:
            dur = await prom_scalar(client, "sum(nv_inference_request_duration_us)")
            cnt = await prom_scalar(client, "sum(nv_inference_request_success)")
            return dur, cnt
        except Exception:
            return 0.0, 0.0

def server_avg_ms(before: Tuple[float, float], after: Tuple[float, float]) -> float:
    d_us, d_cnt = after[0] - before[0], after[1] - before[1]
    return d_us / d_cnt / 1000.0 if d_cnt > 0 else -1.0

async def run_traffic_phase(
    phase_name: str,
    target_rps: int,
    duration_s: int
) -> PhaseResult:
    """Send `target_rps` requests concurrently once per second.

    Bursts (instead of a smooth stream) make the number of in-flight requests
    equal to the target RPS, which is what the engine's batch capacity
    (max_num_seqs) is compared against. That is what degrades latency under
    load and what the hot-reload fixes.
    """
    print(f"\n[FAZA] {phase_name}: {target_rps} RPS (serie rownoczesne) przez {duration_s}s...")
    latencies: List[float] = []
    success = 0
    failures = 0
    start_time = time.time()

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=500, max_keepalive_connections=100)) as client:
        while time.time() - start_time < duration_s:
            burst_start = time.time()
            results = await asyncio.gather(*[send_inference_request(client) for _ in range(target_rps)])
            for lat in results:
                if lat >= 0:
                    success += 1
                    latencies.append(lat)
                else:
                    failures += 1
            sleep_needed = 1.0 - (time.time() - burst_start)
            if sleep_needed > 0:
                await asyncio.sleep(sleep_needed)

    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    latencies.sort()
    p95_lat = latencies[int(len(latencies) * 0.95)] if latencies else 0.0

    print(f"   [OK] Wyslano: {success + failures} | Sukces: {success} | Klient sr.: {avg_lat:.2f} ms | Klient P95: {p95_lat:.2f} ms")
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

async def check_prometheus_metrics() -> Dict[str, Any]:
    """Verify scrape targets are healthy and the alert rules are loaded."""
    print("\n[INFO] Sprawdzanie metryk w Prometheusie...")
    info: Dict[str, Any] = {"targets_up": 0, "targets_total": 0, "alert_rules": 0, "firing": [], "ok": False}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=5.0)
            targets = resp.json().get("data", {}).get("activeTargets", [])
            info["targets_total"] = len(targets)
            info["targets_up"] = sum(1 for t in targets if t.get("health") == "up")
            resp = await client.get(f"{PROMETHEUS_URL}/api/v1/rules", timeout=5.0)
            groups = resp.json().get("data", {}).get("groups", [])
            info["alert_rules"] = sum(len(g.get("rules", [])) for g in groups)
            resp = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=5.0)
            alerts = resp.json().get("data", {}).get("alerts", [])
            info["firing"] = sorted({a["labels"].get("alertname") for a in alerts if a.get("state") == "firing"})
            info["ok"] = info["targets_total"] > 0 and info["targets_up"] == info["targets_total"] and info["alert_rules"] > 0
            print(f"   [{'OK' if info['ok'] else 'FAIL'}] Targety up: {info['targets_up']}/{info['targets_total']} | reguly alertow: {info['alert_rules']} | firing: {info['firing']}")
        except Exception as e:
            print(f"   [FAIL] Nie udalo sie polaczyc z Prometheus ({PROMETHEUS_URL}): {e}")
    return info

async def trigger_reconfiguration(strategy: str) -> bool:
    print(f"\n[SRE] Wywolanie rekonfiguracji przez MCP Agent (strategia='{strategy}')...")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{MCP_AGENT_URL}/reconfigure",
                json={"strategy": strategy},
                timeout=10.0
            )
            print(f"   [OK] Odpowiedz MCP Agenta: {resp.status_code} - {resp.text}")
            return resp.status_code == 200
        except Exception as e:
            print(f"   [ERROR] Blad podczas rekonfiguracji: {e}")
            return False

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
    snap0 = await triton_counters()
    res1 = await run_traffic_phase("1. THE CALM (Obciazenie bazowe)", target_rps=20, duration_s=10)
    snap1 = await triton_counters()
    res1.server_avg_latency_ms = server_avg_ms(snap0, snap1)
    results.append(res1)

    # Faza 2: Skok obciazenia (The Storm)
    res2 = await run_traffic_phase("2. THE STORM (Skok obciazenia - degradacja latencji)", target_rps=STORM_RPS, duration_s=15)
    snap2 = await triton_counters()
    res2.server_avg_latency_ms = server_avg_ms(snap1, snap2)
    results.append(res2)

    # Faza 3: Interwencja Agenta SRE (The Resolution)
    reconfigured = await trigger_reconfiguration("aggressive")
    res3 = await run_traffic_phase("3. THE RESOLUTION (Po rekonfiguracji klastra)", target_rps=STORM_RPS, duration_s=15)
    snap3 = await triton_counters()
    res3.server_avg_latency_ms = server_avg_ms(snap2, snap3)
    results.append(res3)

    # Sprawdzenie metryk w Prometheus
    prom = await check_prometheus_metrics()

    # Podsumowanie
    print("\n==========================================================")
    print("PODSUMOWANIE REALIZACJI PROCEDURY TESTOWEJ")
    print("==========================================================")
    print(f"{'Faza testu':<38} | {'RPS':<5} | {'Serwer sr.':<12} | {'Klient sr.':<12} | {'Klient P95':<12} | {'Sukces':<7}")
    print("-" * 100)
    for r in results:
        print(f"{r.phase_name[:38]:<38} | {r.target_rps:<5} | {r.server_avg_latency_ms:>8.2f} ms | {r.avg_latency_ms:>8.2f} ms | {r.p95_latency_ms:>8.2f} ms | {r.successful_requests}/{r.requests_sent}")
    print("==========================================================")

    # Kryteria PASS/FAIL (zgodne z procedura testowa w docs/)
    checks = {
        "baseline_success_rate>=95%": res1.requests_sent > 0 and res1.successful_requests / res1.requests_sent >= 0.95,
        "reconfigure_endpoint_200": reconfigured,
        "post_reconfig_success_rate>=95%": res3.requests_sent > 0 and res3.successful_requests / res3.requests_sent >= 0.95,
        "prometheus_targets_up_and_rules_loaded": prom["ok"],
        # Server-side latency (from Prometheus counters), independent of port-forward overhead
        "storm_degrades_server_latency>2x_baseline_and_>25ms": res2.server_avg_latency_ms > max(25.0, 2.0 * res1.server_avg_latency_ms),
        "hot_reload_restores_latency<30ms": 0 < res3.server_avg_latency_ms < 30.0,
        "hot_reload_improves_latency>50%": 0 < res3.server_avg_latency_ms < 0.5 * res2.server_avg_latency_ms,
    }
    for name, ok in checks.items():
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")

    report_path = os.getenv("REPORT_PATH", "test-report.json")
    report = {
        "phases": [r.__dict__ for r in results],
        "prometheus": prom,
        "checks": checks,
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Raport zapisano do {report_path}")
    # One-line copy so a Job's logs carry the report (see scripts/run_in_cluster_test.sh)
    print("REPORT_JSON " + json.dumps(report))

    if not all(checks.values()):
        print("Testy zakonczone NIEPOWODZENIEM.")
        sys.exit(1)
    print("Testy zakonczone pomyslnie. Dane zarejestrowane w Prometheusie i Grafanie.")

if __name__ == "__main__":
    asyncio.run(main())
