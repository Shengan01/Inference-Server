#!/usr/bin/env python3
"""
Autonomic Inference Cluster - 3-Act Demo Simulation

This script demonstrates the self-healing capabilities of the inference cluster
by simulating a traffic spike and showing how the MCP Agent autonomously
reconfigures the system to maintain performance.

The demo runs in 3 acts:
1. THE CALM - Normal traffic, system is healthy
2. THE STORM - Traffic spike, latency degrades
3. THE RESOLUTION - Agent reconfigures, latency recovers

Usage:
    python simulation.py

Requirements:
    - Docker Compose stack must be running
    - Model must be loaded in Triton
"""

import os
import sys
import time
import asyncio
from dataclasses import dataclass
from typing import Optional, Any
from collections import deque

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.live import Live
from rich.layout import Layout
from rich.text import Text

# Configuration
TRITON_URL = os.getenv("TRITON_URL", "http://localhost:8000")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
MCP_AGENT_URL = os.getenv("MCP_AGENT_URL", "http://localhost:5000")
MODEL_NAME = os.getenv("MODEL_NAME", "finance-qwen")

console = Console()

# Sample prompts
PROMPTS = [
    "Analyze the quarterly earnings report for tech sector.",
    "What is the current market sentiment?",
    "Explain bond yield curve inversion.",
    "Summarize cryptocurrency market trends.",
]


@dataclass
class SimulationMetrics:
    """Track simulation metrics across all acts."""
    current_act: str = "INITIALIZING"
    requests_sent: int = 0
    successful: int = 0
    failed: int = 0
    current_latency_ms: float = 0.0
    current_rps: float = 0.0
    agent_action: str = "Monitoring..."
    system_status: str = "UNKNOWN"
    latency_history: deque = None  # Rolling window of recent latencies
    
    def __post_init__(self):
        self.latency_history = deque(maxlen=20)  # Last 20 requests


async def check_system_ready() -> bool:
    """Verify that all services are up and ready."""
    console.print("\n[bold]Checking system readiness...[/bold]\n")
    
    checks = [
        ("Triton Server", f"{TRITON_URL}/v2/health/live"),
        ("Prometheus", f"{PROMETHEUS_URL}/-/ready"),
        ("MCP Agent", f"{MCP_AGENT_URL}/health"),
    ]
    
    all_ready = True
    async with httpx.AsyncClient(timeout=10.0) as client:
        for name, url in checks:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    console.print(f"  [OK] {name}: [green]Ready[/green]")
                else:
                    console.print(f"  [ERROR] {name}: [red]Not Ready (HTTP {response.status_code})[/red]")
                    all_ready = False
            except Exception as e:
                console.print(f"  [ERROR] {name}: [red]Error - {e}[/red]")
                all_ready = False
    
    return all_ready


async def get_health_from_agent() -> dict:
    """Query the MCP Agent for current health status."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{MCP_AGENT_URL}/status")
            if response.status_code == 200:
                return response.json()
    except Exception:
        pass
    return {"status": "ERROR", "avg_latency_ms": -1}


async def get_config_from_agent() -> dict:
    """Query the MCP Agent for current Triton config."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{MCP_AGENT_URL}/config/batch-size")
            if response.status_code == 200:
                return response.json()
            return {"max_num_seqs": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"max_num_seqs": f"ERR: {str(e)[:20]}"}  # Short error for display


async def trigger_agent_reconfigure(strategy: str) -> dict:
    """Trigger the MCP Agent to reconfigure the server."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # This would normally be done through MCP protocol
            # For demo purposes, we use a direct HTTP endpoint
            response = await client.post(
                f"{MCP_AGENT_URL}/reconfigure",
                json={"strategy": strategy}
            )
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {"success": False, "error": "Unknown error"}


async def trigger_dynamic_reconfigure(target_seqs: int, gpu_memory: float = None) -> dict:
    """Trigger the MCP Agent to reconfigure with specific parameters."""
    try:
        payload = {"target_seqs": target_seqs}
        if gpu_memory is not None:
            payload["gpu_memory"] = gpu_memory
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{MCP_AGENT_URL}/reconfigure/dynamic",
                json=payload
            )
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {"success": False, "error": "Unknown error"}


async def send_request(client: httpx.AsyncClient, prompt: str) -> tuple[bool, float]:
    """Send a single inference request, return (success, latency_ms)."""
    url = f"{TRITON_URL}/v2/models/{MODEL_NAME}/generate"
    # Increased max_tokens to 128 to simulate heavier compute load
    payload = {"text_input": prompt, "parameters": {"max_tokens": 128}}
    
    start = time.perf_counter()
    try:
        response = await client.post(url, json=payload, timeout=30.0)
        latency = (time.perf_counter() - start) * 1000
        return response.status_code == 200, latency
    except Exception:
        latency = (time.perf_counter() - start) * 1000
        return False, latency


async def generate_load(
    rps: int, 
    duration: int, 
    metrics: SimulationMetrics
) -> None:
    """Generate load at the specified RPS for the given duration using concurrent requests."""
    end_time = time.time() + duration
    
    if rps <= 0:
        # Idle phase - just sleep
        await asyncio.sleep(duration)
        return

    interval = 1.0 / rps
    prompt_idx = 0
    pending_tasks = []
    
    async def fire_request(client: httpx.AsyncClient, prompt: str):
        """Fire a request and update metrics when done."""
        success, latency = await send_request(client, prompt)
        metrics.requests_sent += 1
        if success:
            metrics.successful += 1
        else:
            metrics.failed += 1
        # Rolling average of last 20 requests
        metrics.latency_history.append(latency)
        if metrics.latency_history:
            metrics.current_latency_ms = sum(metrics.latency_history) / len(metrics.latency_history)
    
    async with httpx.AsyncClient() as client:
        start_time = time.time()
        
        while time.time() < end_time:
            prompt = PROMPTS[prompt_idx % len(PROMPTS)]
            prompt_idx += 1
            
            # Fire request without waiting (fire-and-forget)
            task = asyncio.create_task(fire_request(client, prompt))
            pending_tasks.append(task)
            
            # Clean up completed tasks periodically
            if len(pending_tasks) > 100:
                pending_tasks = [t for t in pending_tasks if not t.done()]
            
            # Calculate actual RPS
            elapsed = time.time() - start_time
            if elapsed > 0:
                metrics.current_rps = metrics.requests_sent / elapsed
            
            await asyncio.sleep(interval)
        
        # Wait for remaining tasks to complete
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)


def create_dashboard(metrics: SimulationMetrics, batch_size: Any = "N/A") -> Panel:
    """Create a rich dashboard panel showing current state."""
    # Status color
    status_color = {
        "HEALTHY": "green",
        "WARNING": "yellow",
        "CRITICAL": "red",
        "ERROR": "red",
        "UNKNOWN": "dim"
    }.get(metrics.system_status, "white")
    
    # Act color
    act_color = {
        "ACT 1: THE CALM": "green",
        "ACT 2: THE STORM": "red",
        "ACT 3: THE RESOLUTION": "cyan",
    }.get(metrics.current_act, "white")
    
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    
    table.add_row("Current Act", f"[{act_color}]{metrics.current_act}[/{act_color}]")
    table.add_row("System Status", f"[{status_color}]{metrics.system_status}[/{status_color}]")
    table.add_row("Triton Config", f"[bold cyan]max_num_seqs: {batch_size}[/bold cyan]")
    table.add_row("", "")
    table.add_row("Requests Sent", str(metrics.requests_sent))
    table.add_row("Successful", f"[green]{metrics.successful}[/green]")
    table.add_row("Failed", f"[red]{metrics.failed}[/red]")
    table.add_row("", "")
    table.add_row("Current Latency", f"{metrics.current_latency_ms:.1f} ms")
    table.add_row("Current RPS", f"{metrics.current_rps:.1f} req/s")
    table.add_row("", "")
    table.add_row("Agent Action", f"[italic]{metrics.agent_action}[/italic]")
    
    return Panel(
        table,
        title="Autonomic Inference Cluster Demo",
        border_style="blue"
    )


async def run_simulation():
    """
    Run load generation simulation.
    
    This script generates load and displays metrics.
    The LLM Agent (llm_agent) makes autonomous decisions about configuration.
    """
    console.clear()
    console.print(Panel.fit(
        "[bold cyan]Autonomic Nvidia Inference Cluster[/bold cyan]\n"
        "[dim]Load Generator - Decisions made by LLM Agent[/dim]",
        border_style="cyan"
    ))
    
    # Check system readiness
    if not await check_system_ready():
        console.print("\n[bold red][ERROR] System not ready. Please ensure Docker Compose is running.[/bold red]")
        console.print("Run: [cyan]docker compose up -d[/cyan]")
        console.print("Then start LLM agent: [cyan]docker compose --profile autonomous up -d[/cyan]")
        sys.exit(1)
    
    console.print("\n[bold green][OK] All systems ready. Starting load generation...[/bold green]")
    console.print("[dim]Note: Configuration decisions are made by the LLM Agent (llm_agent container)[/dim]\n")
    await asyncio.sleep(2)
    
    metrics = SimulationMetrics()
    last_batch_size = "N/A"
    
    # Define load phases: (name, rps, duration_seconds)
    phases = [
        ("PHASE 1: IDLE", 0, 15),
        ("PHASE 2: LIGHT LOAD", 50, 30),
        ("PHASE 3: MEDIUM LOAD", 500, 30),
        ("PHASE 4: HEAVY LOAD", 2000, 45),
        ("PHASE 5: EXTREME SPIKE", 5000, 45),
        ("PHASE 6: SUSTAINED HIGH", 3000, 45),
        ("PHASE 7: MEDIUM RECOVERY", 500, 30),
        ("PHASE 8: BACK TO IDLE", 5, 20),
    ]
    
    with Live(create_dashboard(metrics), refresh_per_second=4, console=console) as live:
        
        for phase_idx, (phase_name, rps, duration) in enumerate(phases):
            # Update current act
            metrics.current_act = phase_name
            metrics.agent_action = f"Generating {rps} RPS for {duration}s"
            live.update(create_dashboard(metrics, last_batch_size))
            
            # Start load generation for this phase
            phase_task = asyncio.create_task(generate_load(rps, duration, metrics))
            
            while not phase_task.done():
                # Check health and config (monitoring only, no decisions)
                health = await get_health_from_agent()
                config_status = await get_config_from_agent()
                
                current_batch = config_status.get("max_num_seqs", "N/A")
                if current_batch and current_batch != -1:
                    last_batch_size = current_batch
                
                metrics.system_status = health.get("status", "UNKNOWN")
                latency = health.get("avg_latency_ms", -1)
                
                # Just display metrics - LLM Agent makes decisions
                if latency >= 0:
                    metrics.agent_action = f"Latency: {latency:.1f}ms | Config: {last_batch_size} | RPS: {metrics.current_rps:.0f}"
                else:
                    metrics.agent_action = f"Monitoring... Config: {last_batch_size}"
                
                live.update(create_dashboard(metrics, last_batch_size))
                await asyncio.sleep(0.5)
            
            await phase_task
            
            # Phase complete message
            console.print(f"\n[dim]{phase_name} complete[/dim]")
            await asyncio.sleep(1)
    
    # Final summary
    console.print("\n")
    console.print(Panel.fit(
        "[bold green]LOAD GENERATION COMPLETE[/bold green]\n\n"
        "The LLM Agent autonomously managed configuration during the test.\n"
        "Check llm_agent logs for decision reasoning:\n"
        "[cyan]docker logs llm_agent[/cyan]",
        border_style="green"
    ))
    
    # Print final statistics
    console.print("\n[bold]Final Statistics:[/bold]")
    console.print(f"  Total Requests: {metrics.requests_sent}")
    console.print(f"  Success Rate: {metrics.successful / max(1, metrics.requests_sent) * 100:.1f}%")
    console.print(f"  Final Latency: {metrics.current_latency_ms:.1f} ms")


if __name__ == "__main__":
    asyncio.run(run_simulation())

