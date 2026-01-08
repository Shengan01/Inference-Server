"""
Health Status Tool - Queries Prometheus for System Metrics

This tool provides real-time health information about the inference cluster
by querying Prometheus metrics exposed by the Triton server.
"""

import logging
from typing import Dict, Any

import httpx

logger = logging.getLogger(__name__)

# Health thresholds (in milliseconds)
LATENCY_WARNING_THRESHOLD = 30  # ms
LATENCY_CRITICAL_THRESHOLD = 50  # ms
GPU_WARNING_THRESHOLD = 80  # percent
GPU_CRITICAL_THRESHOLD = 95  # percent


async def get_health_status(prometheus_url: str, model_name: str) -> Dict[str, Any]:
    """
    Query Prometheus for current health metrics and return a status summary.
    
    Args:
        prometheus_url: Base URL of Prometheus server (e.g., http://prometheus:9090)
        model_name: Name of the Triton model to monitor
    
    Returns:
        Dictionary with health status and key metrics
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Query latency (rate over last 30 seconds)
        avg_latency_ms = await _query_latency(client, prometheus_url, model_name)
        
        # Query GPU utilization
        gpu_utilization = await _query_gpu_utilization(client, prometheus_url)
        
        # Query queue depth
        queue_depth = await _query_queue_depth(client, prometheus_url, model_name)
        
        # Query throughput
        throughput_rps = await _query_throughput(client, prometheus_url, model_name)
    
    # Determine overall status
    status = _calculate_status(avg_latency_ms, gpu_utilization)
    
    return {
        "status": status,
        "avg_latency_ms": round(avg_latency_ms, 2) if avg_latency_ms >= 0 else -1,
        "gpu_utilization": round(gpu_utilization, 2) if gpu_utilization >= 0 else -1,
        "queue_depth": queue_depth,
        "throughput_rps": round(throughput_rps, 2) if throughput_rps >= 0 else -1
    }


async def _query_latency(
    client: httpx.AsyncClient, 
    prometheus_url: str, 
    model_name: str
) -> float:
    """Query average request latency in milliseconds."""
    # Compute: rate of total duration / rate of request count = avg latency per request
    # Use 15s window for faster response to load changes
    query = f'rate(nv_inference_request_duration_us{{model="{model_name}"}}[15s]) / rate(nv_inference_request_success{{model="{model_name}"}}[15s])'
    
    try:
        response = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query}
        )
        response.raise_for_status()
        data = response.json()
        
        if data["status"] == "success" and data["data"]["result"]:
            # Convert microseconds to milliseconds
            value_us = float(data["data"]["result"][0]["value"][1])
            # Check for valid value (not NaN, not infinity, reasonable range)
            if value_us > 0 and value_us < 60000000:  # Max 60 seconds
                return value_us / 1000.0
        
        # If no recent requests, return 0 (idle)
        return 0.0
            
    except Exception as e:
        logger.warning(f"Error querying latency: {e}")
    
    return -1.0


async def _query_gpu_utilization(
    client: httpx.AsyncClient, 
    prometheus_url: str
) -> float:
    """Query GPU utilization percentage."""
    query = 'nv_gpu_utilization'
    
    try:
        response = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query}
        )
        response.raise_for_status()
        data = response.json()
        
        if data["status"] == "success" and data["data"]["result"]:
            # GPU utilization is typically 0-1, convert to percentage
            value = float(data["data"]["result"][0]["value"][1])
            return value * 100 if value <= 1 else value
            
    except Exception as e:
        logger.warning(f"Error querying GPU utilization: {e}")
    
    return -1.0


async def _query_queue_depth(
    client: httpx.AsyncClient, 
    prometheus_url: str, 
    model_name: str
) -> int:
    """Query current queue depth (pending requests)."""
    query = f'nv_inference_pending_request_count{{model="{model_name}"}}'
    
    try:
        response = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query}
        )
        response.raise_for_status()
        data = response.json()
        
        if data["status"] == "success" and data["data"]["result"]:
            return int(float(data["data"]["result"][0]["value"][1]))
            
    except Exception as e:
        logger.warning(f"Error querying queue depth: {e}")
    
    return -1


async def _query_throughput(
    client: httpx.AsyncClient, 
    prometheus_url: str, 
    model_name: str
) -> float:
    """Query requests per second throughput."""
    # Use 15s window for faster response to load changes
    query = f'rate(nv_inference_request_success{{model="{model_name}"}}[15s])'
    
    try:
        response = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query}
        )
        response.raise_for_status()
        data = response.json()
        
        if data["status"] == "success" and data["data"]["result"]:
            return float(data["data"]["result"][0]["value"][1])
            
    except Exception as e:
        logger.warning(f"Error querying throughput: {e}")
    
    return -1.0


def _calculate_status(avg_latency_ms: float, gpu_utilization: float) -> str:
    """
    Calculate overall health status based on metrics.
    
    Returns:
        "HEALTHY", "WARNING", or "CRITICAL"
    """
    # Check for critical conditions
    if avg_latency_ms >= LATENCY_CRITICAL_THRESHOLD:
        return "CRITICAL"
    if gpu_utilization >= GPU_CRITICAL_THRESHOLD:
        return "CRITICAL"
    
    # Check for warning conditions
    if avg_latency_ms >= LATENCY_WARNING_THRESHOLD:
        return "WARNING"
    if gpu_utilization >= GPU_WARNING_THRESHOLD:
        return "WARNING"
    
    # All metrics are within acceptable ranges
    return "HEALTHY"
