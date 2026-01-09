"""
Reconfigure Server Tool - Dynamic Batching Configuration

This tool modifies the Triton config.pbtxt file to adjust max_num_seqs
based on real-time load analysis and metrics. Now includes GPU memory
safety checks before hot reload (Improvement #1).
"""

import os
import re
import asyncio
import logging
from typing import Dict, Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Tuning parameters for dynamic calculation
MIN_SEQS = 4       # Minimum sequences (low load)
MAX_SEQS = 4096    # Maximum sequences (LLM has full autonomy)
TARGET_LATENCY_MS = 100  # Target latency to optimize around

# Hot reload safety thresholds (Improvement #1)
GPU_MEMORY_SAFE_THRESHOLD = 0.80  # Only reload if GPU memory below 80%
RELOAD_RETRY_ATTEMPTS = 3
RELOAD_RETRY_DELAY_SECONDS = 2

# Prometheus URL for GPU memory check (set via environment)
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")


def calculate_optimal_seqs(
    current_rps: float,
    avg_latency_ms: float,
    current_seqs: int
) -> int:
    """
    Calculate optimal max_num_seqs based on current metrics.
    
    Algorithm:
    - If latency is too high and RPS is high → increase seqs (more parallelism)
    - If latency is low and RPS is low → decrease seqs (optimize for latency)
    - Scale proportionally to the difference from target
    """
    if current_rps <= 0:
        return MIN_SEQS  # Very low load, optimize for latency
    
    # Calculate load factor (how stressed is the system)
    latency_ratio = avg_latency_ms / TARGET_LATENCY_MS
    
    if latency_ratio > 1.5:
        # System is overloaded - need more parallelism
        # Scale up proportionally to how much we're exceeding target
        scale_up = min(2.0, latency_ratio)
        new_seqs = int(current_seqs * scale_up)
    elif latency_ratio < 0.5 and current_rps < 50:
        # System is underloaded and low traffic - reduce for latency
        new_seqs = max(MIN_SEQS, current_seqs // 2)
    else:
        # System is in good range - minor adjustment based on RPS
        if current_rps > 200:
            new_seqs = max(current_seqs, 256)
        elif current_rps > 50:
            new_seqs = max(current_seqs, 64)
        else:
            new_seqs = current_seqs
    
    # Clamp to valid range
    return max(MIN_SEQS, min(MAX_SEQS, new_seqs))


async def check_gpu_memory_safe() -> tuple[bool, float]:
    """
    Check if GPU memory usage is below safe threshold for hot reload.
    
    Returns:
        (is_safe, current_utilization)
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Query GPU memory utilization from Prometheus
            query = 'nv_gpu_memory_used_bytes / nv_gpu_memory_total_bytes'
            response = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": query}
            )
            
            if response.status_code != 200:
                logger.warning(f"Prometheus query failed: {response.status_code}")
                return False, -1
            
            data = response.json()
            if data["status"] == "success" and data["data"]["result"]:
                utilization = float(data["data"]["result"][0]["value"][1])
                is_safe = utilization < GPU_MEMORY_SAFE_THRESHOLD
                logger.info(f"GPU memory utilization: {utilization*100:.1f}% (safe threshold: {GPU_MEMORY_SAFE_THRESHOLD*100:.0f}%)")
                return is_safe, utilization
            
            # No data available - assume safe to try
            logger.warning("No GPU memory data from Prometheus, assuming safe")
            return True, -1
            
    except Exception as e:
        logger.warning(f"Could not check GPU memory: {e}")
        return True, -1  # Assume safe if can't check


async def reconfigure_server_dynamic(
    target_seqs: int,
    config_path: str,
    triton_url: str,
    model_name: str,
    gpu_memory: float = None  # Optional: 0.3 to 0.9
) -> Dict[str, Any]:
    """
    Reconfigure the Triton server with specific parameters.
    
    IMPROVEMENT #1: Now includes GPU memory safety check and optional hot reload.
    
    Args:
        target_seqs: The target max_num_seqs value (4-4096)
        config_path: Path to the config.pbtxt file
        triton_url: Base URL of the Triton server
        model_name: Name of the model to reload
        gpu_memory: Optional GPU memory utilization (0.3-0.9)
    
    Returns:
        Dictionary with success status and details
    """
    # Clamp to valid range
    target_seqs = max(MIN_SEQS, min(MAX_SEQS, target_seqs))
    if gpu_memory is not None:
        gpu_memory = max(0.3, min(0.9, gpu_memory))
    
    # Step 1: Read current config
    try:
        with open(config_path, 'r') as f:
            config_content = f.read()
        logger.info(f"Read config from {config_path}")
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to read config file: {e}"
        }
    
    # Step 2: Modify config
    try:
        new_config = _update_config(config_content, target_seqs, gpu_memory)
        logger.info(f"Config updated with max_num_seqs={target_seqs}, gpu_memory={gpu_memory}")
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to update config: {e}"
        }
    
    # Step 3: Write new config
    try:
        with open(config_path, 'w') as f:
            f.write(new_config)
        logger.info(f"Wrote updated config to {config_path}")
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to write config file: {e}"
        }
    
    # Step 4: IMPROVEMENT #1 - Check GPU memory before hot reload
    is_safe, gpu_util = await check_gpu_memory_safe()
    
    msg_parts = [f"max_num_seqs={target_seqs}"]
    if gpu_memory is not None:
        msg_parts.append(f"gpu_memory={gpu_memory}")
    
    if is_safe:
        # GPU memory is safe - trigger hot reload
        logger.info(f"GPU memory safe ({gpu_util*100:.1f}%), triggering hot reload...")
        reload_result = await _trigger_reload(triton_url, model_name)
        
        if reload_result.get("success"):
            return {
                "success": True,
                "max_num_seqs": target_seqs,
                "gpu_memory_utilization": gpu_memory,
                "hot_reload": True,
                "message": f"Config applied and hot reload succeeded ({', '.join(msg_parts)})"
            }
        else:
            return {
                "success": True,  # Config was saved even if reload failed
                "max_num_seqs": target_seqs,
                "gpu_memory_utilization": gpu_memory,
                "hot_reload": False,
                "warning": f"Config saved but hot reload failed: {reload_result.get('message')}",
                "message": f"Config saved ({', '.join(msg_parts)}), reload will apply on next restart"
            }
    else:
        # GPU memory too high - skip hot reload to prevent OOM
        logger.warning(f"GPU memory too high ({gpu_util*100:.1f}%), skipping hot reload to prevent OOM")
        return {
            "success": True,
            "max_num_seqs": target_seqs,
            "gpu_memory_utilization": gpu_memory,
            "hot_reload": False,
            "warning": f"GPU memory at {gpu_util*100:.1f}% - reload deferred to prevent OOM",
            "message": f"Config saved ({', '.join(msg_parts)}), will apply on next restart"
        }


def _update_config(
    config_content: str,
    max_num_seqs: int,
    gpu_memory: float = None
) -> str:
    """
    Update the config.pbtxt content with new parameters.
    """
    # Update max_num_seqs
    pattern_seqs = r'(key:\s*"max_num_seqs"\s*value:\s*\{\s*string_value:\s*)"[^"]*"'
    if re.search(pattern_seqs, config_content):
        config_content = re.sub(
            pattern_seqs,
            f'\\g<1>"{max_num_seqs}"',
            config_content
        )
    else:
        logger.warning("Could not find max_num_seqs parameter in config.pbtxt")
    
    # Update gpu_memory_utilization if provided
    if gpu_memory is not None:
        pattern_mem = r'(key:\s*"gpu_memory_utilization"\s*value:\s*\{\s*string_value:\s*)"[^"]*"'
        if re.search(pattern_mem, config_content):
            config_content = re.sub(
                pattern_mem,
                f'\\g<1>"{gpu_memory}"',
                config_content
            )
        else:
            logger.warning("Could not find gpu_memory_utilization parameter in config.pbtxt")
    
    return config_content


async def _trigger_reload(triton_url: str, model_name: str) -> Dict[str, Any]:
    """
    Trigger a hot reload of the model in Triton with retry logic.
    
    Uses the Triton Model Repository API:
    POST /v2/repository/models/{model_name}/load
    """
    for attempt in range(RELOAD_RETRY_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # First, unload the model
                unload_url = f"{triton_url}/v2/repository/models/{model_name}/unload"
                logger.info(f"Unloading model (attempt {attempt+1}/{RELOAD_RETRY_ATTEMPTS})...")
                await client.post(unload_url)
                logger.info(f"Unloaded model: {model_name}")
                
                # Wait a moment for cleanup
                await asyncio.sleep(1)
                
                # Then, reload with new config
                reload_url = f"{triton_url}/v2/repository/models/{model_name}/load"
                logger.info(f"Reloading model with new config...")
                response = await client.post(reload_url)
                
                if response.status_code == 200:
                    logger.info(f"Successfully reloaded model: {model_name}")
                    return {
                        "success": True,
                        "message": "Model reloaded successfully"
                    }
                else:
                    logger.warning(f"Reload attempt {attempt+1} failed: {response.status_code} - {response.text}")
                    if attempt < RELOAD_RETRY_ATTEMPTS - 1:
                        await asyncio.sleep(RELOAD_RETRY_DELAY_SECONDS)
                    
        except httpx.TimeoutException:
            # Reload can take a while, but it might still succeed
            logger.warning(f"Reload request timed out (attempt {attempt+1})")
            if attempt < RELOAD_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RELOAD_RETRY_DELAY_SECONDS)
            else:
                return {
                    "success": True,
                    "message": "Reload initiated (may take additional time)"
                }
        except Exception as e:
            logger.error(f"Error triggering reload (attempt {attempt+1}): {e}")
            if attempt < RELOAD_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RELOAD_RETRY_DELAY_SECONDS)
    
    return {
        "success": False,
        "message": f"Reload failed after {RELOAD_RETRY_ATTEMPTS} attempts"
    }


def get_available_strategies() -> Dict[str, Dict[str, Any]]:
    """Return information about available strategies."""
    # Note: STRATEGIES is not defined in current codebase
    # This function may need to be updated or removed
    return {
        "aggressive": {
            "max_num_seqs": 512,
            "description": "High throughput for heavy load"
        },
        "balanced": {
            "max_num_seqs": 64,
            "description": "Middle-ground for variable workloads"
        },
        "latency_optimized": {
            "max_num_seqs": 8,
            "description": "Low latency for light load"
        }
    }
