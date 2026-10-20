#!/usr/bin/env python3
"""
Config Watcher - Real-time Triton Configuration Monitor

Run this in a separate terminal alongside simulation.py to watch
the MCP Agent's changes to the Triton configuration in real-time.

Usage:
    python watch_config.py
"""

import os
import re
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.live import Live

# Path to config file (relative to this script)
CONFIG_PATH = Path(__file__).parent / "models" / "finance-qwen" / "config.pbtxt"

console = Console()


def parse_config(content: str) -> dict:
    """Extract key parameters from config.pbtxt."""
    params = {}
    
    # Extract model name
    match = re.search(r'^name:\s*"([^"]+)"', content, re.MULTILINE)
    if match:
        params["Model Name"] = match.group(1)
    
    # Extract backend
    match = re.search(r'^backend:\s*"([^"]+)"', content, re.MULTILINE)
    if match:
        params["Backend"] = match.group(1)
    
    # Extract max_batch_size
    match = re.search(r'^max_batch_size:\s*(\d+)', content, re.MULTILINE)
    if match:
        params["Max Batch Size"] = match.group(1)
    
    # Extract all parameters from the parameters block
    param_pattern = r'key:\s*"([^"]+)"\s*value:\s*\{\s*string_value:\s*"([^"]+)"'
    for key, value in re.findall(param_pattern, content):
        # Convert key to title case for display
        display_key = key.replace("_", " ").title()
        params[display_key] = value
    
    return params


def create_dashboard(params: dict, last_modified: float) -> Panel:
    """Create a rich panel showing the current configuration."""
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Parameter", style="bold")
    table.add_column("Value", style="green")
    
    # Highlight the key parameter we're watching
    for key, value in params.items():
        if key == "Max Num Seqs":
            table.add_row(f"* {key}", f"[bold yellow]{value}[/bold yellow]")
        else:
            table.add_row(key, value)
    
    # Add last modified time
    mod_time = time.strftime("%H:%M:%S", time.localtime(last_modified))
    table.add_row("", "")
    table.add_row("Last Modified", f"[dim]{mod_time}[/dim]")
    
    return Panel(
        table,
        title="Triton Configuration Monitor",
        subtitle=f"[dim]Watching: {CONFIG_PATH.name}[/dim]",
        border_style="blue"
    )


def main():
    console.clear()
    console.print(Panel.fit(
        "[bold cyan]Triton Config Watcher[/bold cyan]\n"
        "[dim]Monitoring config.pbtxt for MCP Agent changes[/dim]",
        border_style="cyan"
    ))
    
    if not CONFIG_PATH.exists():
        console.print(f"[red]Error: Config file not found at {CONFIG_PATH}[/red]")
        return
    
    console.print(f"\n[green][OK] Watching:[/green] {CONFIG_PATH}")
    console.print("[dim]Press Ctrl+C to exit[/dim]\n")
    
    last_content = ""
    
    with Live(console=console, refresh_per_second=2) as live:
        while True:
            try:
                content = CONFIG_PATH.read_text()
                last_modified = CONFIG_PATH.stat().st_mtime
                
                # Check if content changed
                if content != last_content:
                    if last_content:
                        console.print("[yellow][UPDATE] Configuration changed.[/yellow]")
                    last_content = content
                
                params = parse_config(content)
                live.update(create_dashboard(params, last_modified))
                
                time.sleep(0.5)
                
            except KeyboardInterrupt:
                console.print("\n[dim]Watcher stopped.[/dim]")
                break
            except Exception as e:
                console.print(f"[red]Error reading config: {e}[/red]")
                time.sleep(1)


if __name__ == "__main__":
    main()
