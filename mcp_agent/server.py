"""
MCP Agent Server - The Brain of the Autonomic Inference Cluster

This server exposes tools to an LLM for autonomous system management:
1. get_health_status() - Query system health from Prometheus
2. reconfigure_server() - Modify Triton config and trigger hot reload

Uses FastMCP for the Model Context Protocol implementation.
"""

import os
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP

from tools.health import get_health_status
from tools.reconfigure import reconfigure_server_dynamic

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Environment configuration
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
TRITON_URL = os.getenv("TRITON_URL", "http://triton_server:8000")
MODEL_NAME = os.getenv("MODEL_NAME", "finance-qwen")
CONFIG_PATH = os.getenv("CONFIG_PATH", "/model_repository/finance-qwen/config.pbtxt")

# Initialize FastMCP server
mcp = FastMCP(
    name="Autonomic SRE Agent"
)

# ============================================
# MCP TOOLS
# ============================================

@mcp.tool(
    name="get_health_status",
    description="""
    Query the current health status of the inference cluster.
    
    Returns a JSON object with:
    - status: "HEALTHY", "WARNING", or "CRITICAL"
    - avg_latency_ms: Average request latency in milliseconds
    - gpu_utilization: GPU usage percentage (0-100)
    - queue_depth: Number of requests waiting in queue
    - throughput_rps: Requests processed per second
    
    Use this tool to monitor system performance and detect issues.
    """
)
async def tool_get_health_status() -> dict:
    """Get the current health status of the inference cluster."""
    logger.info("MCP Tool called: get_health_status")
    try:
        result = await get_health_status(PROMETHEUS_URL, MODEL_NAME)
        logger.info(f"Health status: {result}")
        return result
    except Exception as e:
        logger.error(f"Error getting health status: {e}")
        return {
            "status": "ERROR",
            "error": str(e),
            "avg_latency_ms": -1,
            "gpu_utilization": -1,
            "queue_depth": -1,
            "throughput_rps": -1
        }


@mcp.tool(
    name="reconfigure_server",
    description="""
    Reconfigure the Triton inference server with a new strategy.
    
    Available strategies:
    - "aggressive": Optimize for high throughput under heavy load
      - Sets max_queue_delay to 50ms
      - Sets preferred_batch_size to [16, 32, 64]
      - Best when latency is spiking due to request volume
      
    - "latency_optimized": Optimize for low latency
      - Sets max_queue_delay to 1ms
      - Sets preferred_batch_size to [1, 2, 4]
      - Best for normal load or when fast response is critical
      
    - "balanced": Middle-ground configuration
      - Sets max_queue_delay to 10ms
      - Sets preferred_batch_size to [4, 8, 16]
      - Good default for variable workloads
    
    The server will hot-reload the configuration (takes ~2-5 seconds).
    
    Args:
        strategy: One of "aggressive", "latency_optimized", or "balanced"
    
    Returns success/failure status and details of changes made.
    """
)
async def tool_reconfigure_server(strategy: str) -> dict:
    """Reconfigure the Triton inference server with a named strategy."""
    logger.info(f"MCP Tool called: reconfigure_server(strategy={strategy})")
    strategy_map = {
        "aggressive": (512, 0.8),
        "latency_optimized": (8, 0.4),
        "balanced": (64, 0.6),
    }
    if strategy not in strategy_map:
        return {
            "success": False,
            "error": f"Unknown strategy: {strategy}. Available: {list(strategy_map.keys())}"
        }
    target_seqs, gpu_memory = strategy_map[strategy]
    try:
        result = await reconfigure_server_dynamic(
            target_seqs=target_seqs,
            config_path=CONFIG_PATH,
            triton_url=TRITON_URL,
            model_name=MODEL_NAME,
            gpu_memory=gpu_memory,
        )
        result["strategy"] = strategy
        logger.info(f"Reconfigure result: {result}")
        return result
    except Exception as e:
        logger.error(f"Error reconfiguring server: {e}")
        return {"success": False, "error": str(e)}


@mcp.resource("config://current")
async def get_current_config() -> str:
    """Return the current Triton configuration."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return f.read()
    except Exception as e:
        return f"Error reading config: {e}"


@mcp.resource("metrics://summary")
async def get_metrics_summary() -> str:
    """Return a summary of recent metrics."""
    result = await get_health_status(PROMETHEUS_URL, MODEL_NAME)
    return f"""
Current System Status: {result.get('status', 'UNKNOWN')}
Average Latency: {result.get('avg_latency_ms', 'N/A')} ms
GPU Utilization: {result.get('gpu_utilization', 'N/A')}%
Queue Depth: {result.get('queue_depth', 'N/A')}
Throughput: {result.get('throughput_rps', 'N/A')} req/s
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for the FastAPI app."""
    logger.info("Starting MCP Agent Server...")
    logger.info(f"Prometheus URL: {PROMETHEUS_URL}")
    logger.info(f"Triton URL: {TRITON_URL}")
    logger.info(f"Model Name: {MODEL_NAME}")
    yield
    logger.info("Shutting down MCP Agent Server...")

# Create standard FastAPI app
app = FastAPI(title="MCP Agent Health", lifespan=lifespan)

@app.get("/health")
async def health_check():
    """Health check endpoint for Docker."""
    return JSONResponse({"status": "healthy", "service": "mcp_agent"})


@app.get("/status")
async def get_status():
    """Get current system status (convenience endpoint)."""
    result = await get_health_status(PROMETHEUS_URL, MODEL_NAME)
    return JSONResponse(result)


class ReconfigureRequest(BaseModel):
    strategy: str

@app.post("/reconfigure")
async def reconfigure_http(req: ReconfigureRequest):
    """HTTP endpoint to trigger reconfiguration (for demo)."""
    return await tool_reconfigure_server(req.strategy)


@app.get("/metrics/config")
async def get_config_metrics():
    """Expose current configuration as Prometheus-style metrics for Grafana."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            content = f.read()
            import re
            
            # Extract max_num_seqs
            match_seqs = re.search(r'key:\s*"max_num_seqs"\s*value:\s*\{\s*string_value:\s*"(\d+)"', content)
            max_num_seqs = int(match_seqs.group(1)) if match_seqs else -1
            
            # Extract gpu_memory_utilization
            match_gpu = re.search(r'key:\s*"gpu_memory_utilization"\s*value:\s*\{\s*string_value:\s*"([0-9.]+)"', content)
            gpu_memory = float(match_gpu.group(1)) if match_gpu else -1
            
            # Return as Prometheus text format
            metrics = f"""# HELP triton_config_max_num_seqs Current max_num_seqs configuration
# TYPE triton_config_max_num_seqs gauge
triton_config_max_num_seqs {max_num_seqs}
# HELP triton_config_gpu_memory Current gpu_memory_utilization configuration
# TYPE triton_config_gpu_memory gauge
triton_config_gpu_memory {gpu_memory}
"""
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(metrics, media_type="text/plain")
    except Exception as e:
        return PlainTextResponse(f"# Error: {e}", media_type="text/plain")


@app.get("/config/batch-size")
async def get_batch_config():
    """Get the current max_num_seqs from the config file."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            content = f.read()
            # Simple regex to extract max_num_seqs
            import re
            match = re.search(r'key:\s*"max_num_seqs"\s*value:\s*\{\s*string_value:\s*"(\d+)"', content)
            if match:
                return {"max_num_seqs": int(match.group(1))}
            return {"max_num_seqs": -1, "error": "Not found"}
    except Exception as e:
        return {"max_num_seqs": -1, "error": str(e)}


class DynamicReconfigureRequest(BaseModel):
    target_seqs: int
    gpu_memory: float = None  # Optional: 0.3 to 0.9


@app.post("/reconfigure/dynamic")
async def reconfigure_dynamic_http(req: DynamicReconfigureRequest):
    """HTTP endpoint to reconfigure with specific parameters."""
    from tools.reconfigure import reconfigure_server_dynamic
    
    logger.info(f"Dynamic reconfigure called with target_seqs={req.target_seqs}, gpu_memory={req.gpu_memory}")
    result = await reconfigure_server_dynamic(
        target_seqs=req.target_seqs,
        config_path=CONFIG_PATH,
        triton_url=TRITON_URL,
        model_name=MODEL_NAME,
        gpu_memory=req.gpu_memory
    )
    logger.info(f"Dynamic reconfigure result: {result}")
    return result


# Mount MCP server app (Starlette) at root
# This allows standard MCP endpoints like /sse and /messages to work
mcp_app = mcp.streamable_http_app()
app.mount("/", mcp_app)


if __name__ == "__main__":
    # Run with both HTTP health checks and MCP SSE
    logger.info("Starting MCP Agent on port 5000...")
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=5000,
        reload=False,
        log_level="info"
    )
