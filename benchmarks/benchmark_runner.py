#!/usr/bin/env python3
"""
Benchmark Runner for Autonomous Inference Server

Runs comprehensive performance benchmarks across different load scenarios
and collects metrics for analysis and visualization.

Usage:
    python benchmark_runner.py --duration 180 --output results/
    python benchmark_runner.py --quick  # Quick 30s tests
"""

import os
import sys
import json
import time
import asyncio
import argparse
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from pathlib import Path

import httpx

# Configuration
TRITON_URL = os.getenv("TRITON_URL", "http://localhost:8000")
MCP_AGENT_URL = os.getenv("MCP_AGENT_URL", "http://localhost:5000")
MODEL_NAME = os.getenv("MODEL_NAME", "finance-qwen")


@dataclass
class BenchmarkMetrics:
    """Metrics collected during a benchmark run."""
    timestamp: str
    scenario: str
    target_rps: int
    duration_seconds: int
    
    # Request metrics
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    
    # Latency metrics (milliseconds)
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    min_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    
    # Throughput
    actual_rps: float = 0.0
    
    # System state
    final_max_num_seqs: int = 0
    agent_reconfigurations: int = 0
    
    # Raw latencies for percentile calculation
    latencies: List[float] = field(default_factory=list)
    
    def calculate_percentiles(self):
        """Calculate latency percentiles from raw data."""
        if not self.latencies:
            return
        
        sorted_lat = sorted(self.latencies)
        n = len(sorted_lat)
        
        self.min_latency_ms = sorted_lat[0]
        self.max_latency_ms = sorted_lat[-1]
        self.avg_latency_ms = sum(sorted_lat) / n
        self.p50_latency_ms = sorted_lat[int(n * 0.50)]
        self.p95_latency_ms = sorted_lat[int(n * 0.95)]
        self.p99_latency_ms = sorted_lat[min(int(n * 0.99), n - 1)]
    
    def to_dict(self) -> dict:
        """Convert to dictionary, excluding raw latencies."""
        self.calculate_percentiles()
        data = asdict(self)
        data.pop('latencies', None)
        return data


@dataclass
class BenchmarkScenario:
    """A benchmark scenario configuration."""
    name: str
    target_rps: int
    duration: int  # seconds
    description: str


# Default scenarios covering the full range
DEFAULT_SCENARIOS = [
    BenchmarkScenario("idle", 0, 15, "System idle - agent should scale down"),
    BenchmarkScenario("light_load", 50, 30, "Light load - baseline performance"),
    BenchmarkScenario("moderate_load", 200, 45, "Moderate load - testing throughput"),
    BenchmarkScenario("heavy_load", 500, 60, "Heavy load - stress testing"),
    BenchmarkScenario("extreme_spike", 1000, 45, "Extreme spike - max capacity"),
    BenchmarkScenario("recovery", 100, 30, "Recovery phase - agent adapt down"),
]

QUICK_SCENARIOS = [
    BenchmarkScenario("light_load", 50, 15, "Quick light load test"),
    BenchmarkScenario("heavy_load", 500, 20, "Quick heavy load test"),
    BenchmarkScenario("recovery", 50, 10, "Quick recovery"),
]


async def send_request(
    client: httpx.AsyncClient,
    prompt: str
) -> tuple[bool, float]:
    """Send a single inference request and return (success, latency_ms)."""
    url = f"{TRITON_URL}/v2/models/{MODEL_NAME}/generate"
    payload = {"text_input": prompt, "parameters": {"max_tokens": 50}}
    
    start = time.perf_counter()
    try:
        response = await client.post(url, json=payload, timeout=30.0)
        latency = (time.perf_counter() - start) * 1000
        return response.status_code == 200, latency
    except Exception:
        latency = (time.perf_counter() - start) * 1000
        return False, latency


async def get_current_config() -> dict:
    """Get current server configuration from MCP Agent."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{MCP_AGENT_URL}/config/batch-size")
            if response.status_code == 200:
                return response.json()
    except Exception:
        pass
    return {"max_num_seqs": -1}


async def run_scenario(
    scenario: BenchmarkScenario,
    progress_callback=None
) -> BenchmarkMetrics:
    """Run a single benchmark scenario."""
    metrics = BenchmarkMetrics(
        timestamp=datetime.now().isoformat(),
        scenario=scenario.name,
        target_rps=scenario.target_rps,
        duration_seconds=scenario.duration
    )
    
    if scenario.target_rps == 0:
        # Idle scenario - just wait
        print(f"Idle phase for {scenario.duration}s...")
        await asyncio.sleep(scenario.duration)
        config = await get_current_config()
        metrics.final_max_num_seqs = config.get("max_num_seqs", -1)
        return metrics
    
    prompts = [
        "Analyze the quarterly earnings report.",
        "What is the market sentiment?",
        "Explain bond yield curve.",
        "Summarize cryptocurrency trends.",
    ]
    
    interval = 1.0 / scenario.target_rps
    end_time = time.time() + scenario.duration
    prompt_idx = 0
    pending_tasks = []
    
    async def fire_request(client: httpx.AsyncClient, prompt: str):
        """Fire a request and record metrics."""
        success, latency = await send_request(client, prompt)
        metrics.total_requests += 1
        if success:
            metrics.successful_requests += 1
            metrics.latencies.append(latency)
        else:
            metrics.failed_requests += 1
    
    start_time = time.time()
    
    async with httpx.AsyncClient() as client:
        while time.time() < end_time:
            prompt = prompts[prompt_idx % len(prompts)]
            prompt_idx += 1
            
            task = asyncio.create_task(fire_request(client, prompt))
            pending_tasks.append(task)
            
            # Cleanup completed tasks
            if len(pending_tasks) > 200:
                pending_tasks = [t for t in pending_tasks if not t.done()]
            
            elapsed = time.time() - start_time
            if elapsed > 0 and progress_callback:
                progress_callback(elapsed / scenario.duration)
            
            await asyncio.sleep(interval)
        
        # Wait for remaining tasks
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
    
    # Calculate final metrics
    elapsed = time.time() - start_time
    metrics.actual_rps = metrics.successful_requests / elapsed if elapsed > 0 else 0
    
    # Get final config
    config = await get_current_config()
    metrics.final_max_num_seqs = config.get("max_num_seqs", -1)
    
    return metrics


async def run_benchmark(
    scenarios: List[BenchmarkScenario],
    output_dir: Path
) -> List[BenchmarkMetrics]:
    """Run all benchmark scenarios and collect results."""
    results = []
    
    print("\n" + "=" * 60)
    print("AUTONOMOUS INFERENCE SERVER BENCHMARK")
    print("=" * 60)
    print(f"Triton URL: {TRITON_URL}")
    print(f"Model: {MODEL_NAME}")
    print(f"Scenarios: {len(scenarios)}")
    print("=" * 60 + "\n")
    
    for i, scenario in enumerate(scenarios, 1):
        print(f"\n[{i}/{len(scenarios)}] {scenario.name.upper()}")
        print(f"    Target RPS: {scenario.target_rps}")
        print(f"    Duration: {scenario.duration}s")
        print(f"    {scenario.description}")
        
        metrics = await run_scenario(scenario)
        results.append(metrics)
        
        # Print summary
        if metrics.latencies:
            metrics.calculate_percentiles()
            print(f"Completed: {metrics.successful_requests} requests")
            print(f"Avg Latency: {metrics.avg_latency_ms:.1f}ms")
            print(f"P99 Latency: {metrics.p99_latency_ms:.1f}ms")
            print(f"Actual RPS: {metrics.actual_rps:.1f}")
            print(f"Final max_num_seqs: {metrics.final_max_num_seqs}")
        else:
            print(f"Idle - Final max_num_seqs: {metrics.final_max_num_seqs}")
    
    # Save results
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"benchmark_{timestamp}.json"
    
    with open(output_file, 'w') as f:
        json.dump([m.to_dict() for m in results], f, indent=2)
    
    print(f"\nResults saved to: {output_file}")
    
    return results


def print_summary(results: List[BenchmarkMetrics]):
    """Print a summary table of benchmark results."""
    print("\n" + "=" * 80)
    print("BENCHMARK SUMMARY")
    print("=" * 80)
    print(f"{'Scenario':<20} {'RPS':<10} {'Avg (ms)':<12} {'P99 (ms)':<12} {'Success %':<12} {'Config':<10}")
    print("-" * 80)
    
    for m in results:
        if m.total_requests > 0:
            success_rate = (m.successful_requests / m.total_requests) * 100
            print(f"{m.scenario:<20} {m.actual_rps:<10.1f} {m.avg_latency_ms:<12.1f} "
                  f"{m.p99_latency_ms:<12.1f} {success_rate:<12.1f} {m.final_max_num_seqs:<10}")
        else:
            print(f"{m.scenario:<20} {'idle':<10} {'-':<12} {'-':<12} {'-':<12} {m.final_max_num_seqs:<10}")
    
    print("=" * 80)


async def main():
    parser = argparse.ArgumentParser(description="Run inference server benchmarks")
    parser.add_argument("--output", "-o", default="benchmarks/results",
                        help="Output directory for results")
    parser.add_argument("--quick", action="store_true",
                        help="Run quick 30s scenarios instead of full benchmark")
    parser.add_argument("--duration", type=int, default=None,
                        help="Override duration for all scenarios (seconds)")
    args = parser.parse_args()
    
    scenarios = QUICK_SCENARIOS if args.quick else DEFAULT_SCENARIOS
    
    if args.duration:
        scenarios = [
            BenchmarkScenario(s.name, s.target_rps, args.duration, s.description)
            for s in scenarios
        ]
    
    output_dir = Path(args.output)
    results = await run_benchmark(scenarios, output_dir)
    print_summary(results)


if __name__ == "__main__":
    asyncio.run(main())
