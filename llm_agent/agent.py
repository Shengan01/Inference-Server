#!/usr/bin/env python3
"""
Autonomous LLM Agent using Ollama with MCP Protocol Integration

This agent monitors the Triton inference server via the MCP Agent
and uses a local LLM (Qwen2.5:3b via Ollama) to make autonomous
decisions about server configuration.

The agent:
1. Polls health metrics from MCP Agent (via MCP SSE or HTTP fallback)
2. Sends context to Ollama LLM
3. LLM reasons about optimal configuration
4. Agent executes the LLM's decision via MCP tools
5. Validates improvement and rolls back if needed (FEEDBACK LOOP)

"""

import os
import json
import time
import asyncio
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Any

import httpx

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logging.warning("MCP package not available, using HTTP fallback")

try:
    from learning import get_optimizer, compute_reward
    RL_AVAILABLE = True
except ImportError:
    RL_AVAILABLE = False
    logging.warning("RL learning module not available")

# Configuration
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
MCP_AGENT_URL = os.getenv("MCP_AGENT_URL", "http://localhost:5000")
DECISION_INTERVAL = int(os.getenv("DECISION_INTERVAL", "3"))  # seconds between checks
USE_MCP_PROTOCOL = os.getenv("USE_MCP_PROTOCOL", "true").lower() == "true"

THRESHOLDS = {
    # Latency targets (in milliseconds) - Tuned for fast hardware
    "latency_critical": 10,   # Above this: SCALE UP immediately
    "latency_high": 8,        # Above this: Consider scaling up
    "latency_target": 5,      # Optimal target
    "latency_low": 3,         # Below this: Can save power
    
    # Load thresholds (RPS)
    "rps_extreme": 500,
    "rps_heavy": 100,
    "rps_moderate": 20,
    "rps_idle": 5,
    
    # GPU thresholds
    "gpu_critical": 95,
    "gpu_warning": 80,
    
    # Configuration bounds
    "max_num_seqs_min": 4,
    "max_num_seqs_max": 4096,
    "gpu_memory_min": 0.3,
    "gpu_memory_max": 0.9,
    
    # Feedback loop
    "rollback_degradation_threshold": 1.5,  # 50% worse triggers rollback
    "validation_wait_seconds": 9,           # Wait time after config change for traffic to stabilize
}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("llm_agent")


@dataclass
class ConfigState:
    """Track configuration state for rollback capability."""
    max_num_seqs: Optional[int] = None
    gpu_memory: Optional[float] = None
    applied_at: Optional[datetime] = None
    pre_change_latency: Optional[float] = None
    
    def to_dict(self) -> dict:
        return {
            "max_num_seqs": self.max_num_seqs,
            "gpu_memory": self.gpu_memory
        }


@dataclass
class AgentState:
    """Track agent state including history for feedback loop."""
    previous_config: Optional[ConfigState] = None
    current_config: Optional[ConfigState] = None
    pending_validation: bool = False
    decision_history: List[dict] = field(default_factory=list)
    mcp_session: Any = None  # MCP ClientSession if available
    
    def record_decision(self, decision: dict, metrics: dict, result: dict):
        """Record a decision for learning and debugging."""
        self.decision_history.append({
            "timestamp": datetime.now().isoformat(),
            "metrics": metrics,
            "decision": decision,
            "result": result
        })
        # Keep only last 100 decisions
        if len(self.decision_history) > 100:
            self.decision_history = self.decision_history[-100:]


# System prompt for the FULLY AUTONOMOUS agent
SYSTEM_PROMPT = """You are an autonomous infrastructure agent managing a GPU inference server.
Your job is to autonomously decide the optimal configuration at every moment.
You have FULL control — there are no rules overriding your decisions.

=== WHAT YOU CONTROL ===
- max_num_seqs: Parallel sequences (8 to 4096). Higher = more throughput capacity but more resource usage.
- gpu_memory: GPU memory allocation (0.3 to 0.9). Higher = more VRAM reserved.

=== HOW THE SERVER WORKS ===
Higher max_num_seqs lets the server handle MORE concurrent requests. This means:
- If latency is ABOVE 5ms → INCREASE max_num_seqs to give the server more capacity
- If latency is BELOW 5ms → the current config is WORKING. Do NOT increase resources just because latency is low!
- Low latency does NOT mean "room to grow". It means "things are fine, don't break it."

=== YOUR DUAL OBJECTIVES ===
1. PERFORMANCE: Keep average latency under 5ms
2. EFFICIENCY: Use the minimum resources that satisfy objective #1

Both matter equally. Wasting GPU on an idle server is as bad as letting latency spike.

=== KEY PRINCIPLE: PROPORTIONALITY ===

Resources should be PROPORTIONAL to load. Ask yourself:
"Is my current configuration the smallest that can handle this load with latency under 5ms?"

- If latency is ABOVE 5ms → you need MORE resources, not fewer!
- If latency is BELOW 5ms AND load is low AND resources are high → scale DOWN to save resources
- If latency is BELOW 5ms AND resources seem matched to load → no_action

IMPORTANT: If latency is already under 5ms, there is NEVER a reason to scale UP.
Scaling up when latency is fine wastes resources and can actually hurt performance.

=== WHEN TO USE EACH ACTION ===

"reconfigure" (SCALE UP): ONLY when latency is above 5ms. Never scale up if latency is fine.
"reconfigure" (SCALE DOWN): When latency is well under 5ms AND current resources are higher than needed for the load.
"no_action": When latency is under 5ms and configuration seems reasonable for the load. This is often the best choice.

=== RESPONSE FORMAT ===
You MUST respond with valid JSON only, no other text:
{"reasoning": "your analysis of the situation", "action": "reconfigure" or "no_action", "max_num_seqs": <int 8-4096>, "gpu_memory": <float 0.3-0.9>}"""


class MCPClient:
    """
    MCP Protocol Client for communicating with MCP Agent.
    
    Uses SSE (Server-Sent Events) transport for real MCP protocol.
    Falls back to HTTP if MCP is unavailable.
    """
    
    def __init__(self, mcp_url: str):
        self.mcp_url = mcp_url
        self.sse_url = f"{mcp_url}/sse"
        self.session: Optional[ClientSession] = None
        self._connected = False
    
    async def connect(self) -> bool:
        """Connect to MCP server via SSE."""
        if not MCP_AVAILABLE:
            logger.info("MCP not available, using HTTP fallback")
            return False
        
        try:
            logger.info(f"Connecting to MCP server at {self.sse_url}...")
            self._connected = True
            logger.info("MCP SSE client ready (will use per-request connections)")
            return True
        except Exception as e:
            logger.warning(f"Failed to connect to MCP: {e}, using HTTP fallback")
            return False
    
    async def call_tool(self, tool_name: str, arguments: dict = None) -> dict:
        """
        Call an MCP tool.
        
        For demo purposes, we use HTTP since persistent SSE connections
        are complex to maintain. The structure follows MCP semantics.
        """
        if arguments is None:
            arguments = {}
        
        # Log MCP-style call
        logger.info(f"MCP Tool Call: {tool_name}({arguments})")
        
        # Map tool names to HTTP endpoints (HTTP implementation of MCP semantics)
        tool_endpoints = {
            "get_health_status": ("GET", "/status", None),
            "reconfigure_server": ("POST", "/reconfigure", {"strategy": arguments.get("strategy", "balanced")}),
        }
        
        if tool_name not in tool_endpoints:
            return {"error": f"Unknown tool: {tool_name}"}
        
        method, endpoint, payload = tool_endpoints[tool_name]
        url = f"{self.mcp_url}{endpoint}"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    response = await client.get(url)
                else:
                    response = await client.post(url, json=payload)
                
                if response.status_code == 200:
                    return response.json()
                else:
                    return {"error": f"HTTP {response.status_code}: {response.text}"}
        except Exception as e:
            return {"error": str(e)}
    
    async def get_resource(self, resource_uri: str) -> str:
        """
        Get an MCP resource.
        
        Resources are read-only data exposed by the MCP server.
        """
        logger.info(f"MCP Resource: {resource_uri}")
        
        # Map resource URIs to HTTP endpoints
        if resource_uri == "config://current":
            url = f"{self.mcp_url}/config/batch-size"
        elif resource_uri == "metrics://summary":
            url = f"{self.mcp_url}/status"
        else:
            return f"Unknown resource: {resource_uri}"
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    return json.dumps(response.json())
                else:
                    return f"Error: HTTP {response.status_code}"
        except Exception as e:
            return f"Error: {str(e)}"


# Global MCP client instance
mcp_client: Optional[MCPClient] = None


async def get_metrics() -> dict:
    """Fetch current metrics from MCP Agent using MCP protocol or HTTP fallback."""
    global mcp_client
    
    if mcp_client and USE_MCP_PROTOCOL:
        # Use MCP protocol (Improvement #7)
        try:
            health_result = await mcp_client.call_tool("get_health_status")
            config_resource = await mcp_client.get_resource("config://current")
            
            try:
                config = json.loads(config_resource)
            except:
                config = {}
            
            return {
                "status": health_result.get("status", "UNKNOWN"),
                "avg_latency_ms": health_result.get("avg_latency_ms", -1),
                "throughput_rps": health_result.get("throughput_rps", 0),
                "gpu_utilization": health_result.get("gpu_utilization", -1),
                "queue_depth": health_result.get("queue_depth", 0),
                "current_max_num_seqs": config.get("max_num_seqs", -1),
                "timestamp": datetime.now().isoformat(),
                "source": "mcp"
            }
        except Exception as e:
            logger.warning(f"MCP call failed, falling back to HTTP: {e}")
    
    # HTTP fallback
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            health_resp = await client.get(f"{MCP_AGENT_URL}/status")
            health = health_resp.json() if health_resp.status_code == 200 else {}
            
            config_resp = await client.get(f"{MCP_AGENT_URL}/config/batch-size")
            config = config_resp.json() if config_resp.status_code == 200 else {}
            
            return {
                "status": health.get("status", "UNKNOWN"),
                "avg_latency_ms": health.get("avg_latency_ms", -1),
                "throughput_rps": health.get("throughput_rps", 0),
                "gpu_utilization": health.get("gpu_utilization", -1),
                "queue_depth": health.get("queue_depth", 0),
                "current_max_num_seqs": config.get("max_num_seqs", -1),
                "timestamp": datetime.now().isoformat(),
                "source": "http"
            }
    except Exception as e:
        logger.error(f"Failed to fetch metrics: {e}")
        return {"error": str(e)}


async def apply_config(max_num_seqs: int = None, gpu_memory: float = None) -> dict:
    """Apply configuration changes via MCP Agent."""
    if max_num_seqs is None and gpu_memory is None:
        return {"success": True, "message": "No changes to apply"}
    
    try:
        payload = {}
        if max_num_seqs is not None:
            payload["target_seqs"] = max_num_seqs
        if gpu_memory is not None:
            payload["gpu_memory"] = gpu_memory
        
        # Use default seqs if only changing GPU memory
        if "target_seqs" not in payload:
            payload["target_seqs"] = 64  # Default
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{MCP_AGENT_URL}/reconfigure/dynamic",
                json=payload
            )
            return response.json() if response.status_code == 200 else {"error": response.text}
    except Exception as e:
        logger.error(f"Failed to apply config: {e}")
        return {"error": str(e)}


def calculate_latency_state(latency: float) -> str:
    """Calculate latency state based on unified thresholds."""
    if latency > THRESHOLDS["latency_critical"]:
        return "CRITICAL"
    elif latency > THRESHOLDS["latency_high"]:
        return "HIGH"
    elif latency > THRESHOLDS["latency_target"]:
        return "MODERATE"
    elif latency > THRESHOLDS["latency_low"]:
        return "GOOD"
    else:
        return "LOW"


def calculate_load_state(rps: float) -> str:
    """Calculate load state based on unified thresholds."""
    if rps > THRESHOLDS["rps_extreme"]:
        return "EXTREME"
    elif rps > THRESHOLDS["rps_heavy"]:
        return "HEAVY"
    elif rps > THRESHOLDS["rps_moderate"]:
        return "MODERATE"
    elif rps > THRESHOLDS["rps_idle"]:
        return "LIGHT"
    else:
        return "IDLE"


async def ask_llm(metrics: dict) -> dict:
    """Ask Ollama LLM for a decision based on current metrics."""
    
    latency = metrics.get('avg_latency_ms', 0)
    rps = metrics.get('throughput_rps', 0)
    
    latency_state = calculate_latency_state(latency)
    load_state = calculate_load_state(rps)
    
    user_prompt = f"""Current server metrics:
- Average Latency: {latency:.1f} ms
- Throughput: {rps:.1f} requests/second
- GPU Utilization: {metrics.get('gpu_utilization', 'N/A')}%
- Queue Depth: {metrics.get('queue_depth', 'N/A')}
- Current max_num_seqs: {metrics.get('current_max_num_seqs', 'N/A')}

What should the configuration be? Respond with JSON only."""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": user_prompt,
                    "system": SYSTEM_PROMPT,
                    "stream": False,
                    "format": "json"
                }
            )
            
            if response.status_code != 200:
                logger.error(f"Ollama error: {response.status_code} - {response.text}")
                return {"action": "no_action", "reasoning": "LLM unavailable"}
            
            result = response.json()
            llm_response = result.get("response", "{}")
            
            # Parse JSON from LLM response
            try:
                decision = json.loads(llm_response)
                return decision
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse LLM response as JSON: {llm_response}")
                return {"action": "no_action", "reasoning": "Invalid LLM response"}
                
    except Exception as e:
        logger.error(f"Failed to query Ollama: {e}")
        return {"action": "no_action", "reasoning": f"Error: {str(e)}"}


async def validate_and_maybe_rollback(
    agent_state: AgentState,
    previous_latency: float
) -> bool:
    """
    FEEDBACK LOOP (Improvement #2)
    
    Validate that the configuration change improved performance.
    If performance degraded significantly, roll back to previous config.
    
    Returns True if validation passed, False if rollback was triggered.
    """
    if agent_state.previous_config is None:
        logger.info("No previous config to compare against, skipping validation")
        return True
    
    # Wait for new config to take effect
    wait_time = THRESHOLDS["validation_wait_seconds"]
    logger.info(f"Waiting {wait_time}s for config to take effect...")
    await asyncio.sleep(wait_time)
    
    # Get new metrics
    new_metrics = await get_metrics()
    if "error" in new_metrics:
        logger.warning("Could not get metrics for validation")
        return True  # Assume OK if can't validate
    
    new_latency = new_metrics.get("avg_latency_ms", -1)
    
    if new_latency < 0 or previous_latency <= 0:
        logger.info("No latency data for comparison")
        return True
    
    degradation_ratio = new_latency / previous_latency
    threshold = THRESHOLDS["rollback_degradation_threshold"]
    
    logger.info(f"Validation: Previous={previous_latency:.1f}ms, New={new_latency:.1f}ms, Ratio={degradation_ratio:.2f}")
    
    if degradation_ratio > threshold and new_latency > THRESHOLDS['latency_target']:
        # Performance degraded significantly AND latency is above target - ROLLBACK
        logger.warning(f"ROLLBACK: Latency degraded by {(degradation_ratio-1)*100:.0f}% (threshold: {(threshold-1)*100:.0f}%)")
        
        prev = agent_state.previous_config
        rollback_result = await apply_config(prev.max_num_seqs, prev.gpu_memory)
        
        if rollback_result.get("success"):
            logger.info(f"Rolled back to: seqs={prev.max_num_seqs}, gpu={prev.gpu_memory}")
            agent_state.current_config = agent_state.previous_config
        else:
            logger.error(f"Rollback failed: {rollback_result}")
        
        return False
    
    logger.info("Validation passed: Configuration improved or maintained performance")
    return True


async def run_agent_loop():
    """Main agent loop - observe, reason, act, VALIDATE."""
    global mcp_client
    
    logger.info(f"Starting LLM Agent with Feedback Loop")
    logger.info(f"  Ollama URL: {OLLAMA_URL}")
    logger.info(f"  Ollama Model: {OLLAMA_MODEL}")
    logger.info(f"  MCP Agent URL: {MCP_AGENT_URL}")
    logger.info(f"  Decision Interval: {DECISION_INTERVAL}s")
    logger.info(f"  Use MCP Protocol: {USE_MCP_PROTOCOL}")
    logger.info(f"  MCP Available: {MCP_AVAILABLE}")
    logger.info(f"  Thresholds: {json.dumps(THRESHOLDS, indent=2)}")
    
    # Initialize MCP client
    if USE_MCP_PROTOCOL:
        mcp_client = MCPClient(MCP_AGENT_URL)
        await mcp_client.connect()
    
    # Initialize agent state for feedback loop
    agent_state = AgentState()
    
    # Wait for services to be ready
    logger.info("Waiting for services...")
    await asyncio.sleep(5)
    
    while True:
        try:
            # 1. OBSERVE - Get current metrics
            logger.info("Fetching metrics...")
            metrics = await get_metrics()
            
            if "error" in metrics:
                logger.warning(f"Could not fetch metrics: {metrics['error']}")
                await asyncio.sleep(DECISION_INTERVAL)
                continue
            
            current_latency = metrics['avg_latency_ms']
            source = metrics.get('source', 'unknown')
            logger.info(f"   [{source.upper()}] Status: {metrics['status']}, Latency: {current_latency:.1f}ms, "
                       f"RPS: {metrics['throughput_rps']:.1f}, Seqs: {metrics['current_max_num_seqs']}")
            
            # 2. REASON - Ask LLM for decision
            logger.info("Asking LLM for decision...")
            decision = await ask_llm(metrics)  # LLM has FULL autonomy - no overrides!
            
            logger.info(f"   LLM Decision: {decision.get('action', 'unknown')}")
            logger.info(f"   Reasoning: {decision.get('reasoning', 'none')}")
            
            # 3. ACT - Apply changes if needed
            if decision.get("action") == "reconfigure":
                new_seqs = decision.get("max_num_seqs")
                new_gpu = decision.get("gpu_memory")
                
                # No-op detection: skip if LLM proposed the same config
                current_seqs = metrics.get('current_max_num_seqs')
                if (new_seqs is not None and current_seqs is not None
                        and isinstance(current_seqs, (int, float)) and int(new_seqs) == int(current_seqs)):
                    logger.info(f"Skipping reconfigure: LLM proposed seqs={new_seqs} which is already the current value")
                elif new_seqs is not None or new_gpu is not None:
                    # Store current config for potential rollback (FEEDBACK LOOP)
                    agent_state.previous_config = ConfigState(
                        max_num_seqs=metrics.get('current_max_num_seqs'),
                        gpu_memory=None,  # We don't track this currently
                        applied_at=datetime.now(),
                        pre_change_latency=current_latency
                    )
                    
                    logger.info(f"Applying config: seqs={new_seqs}, gpu={new_gpu}")
                    result = await apply_config(new_seqs, new_gpu)
                    
                    if result.get("success"):
                        logger.info(f"Config applied successfully")
                        if result.get("hot_reload"):
                            logger.info(f"Hot reload: {result.get('message', 'success')}")
                        else:
                            logger.info(f"Config saved: {result.get('message', 'pending restart')}")
                        
                        # Update current config
                        agent_state.current_config = ConfigState(
                            max_num_seqs=new_seqs,
                            gpu_memory=new_gpu,
                            applied_at=datetime.now()
                        )
                        
                        # Record decision
                        agent_state.record_decision(decision, metrics, result)
                        
                        # 4. VALIDATE - Check if change improved things (FEEDBACK LOOP)
                        validation_passed = await validate_and_maybe_rollback(agent_state, current_latency)
                        
                        # 5. RL LEARNING - Record experience for future optimization
                        if RL_AVAILABLE:
                            try:
                                # Get post-change metrics for reward calculation
                                new_metrics = await get_metrics()
                                new_latency = new_metrics.get('avg_latency_ms', current_latency)
                                
                                reward = compute_reward(
                                    prev_latency_ms=current_latency,
                                    new_latency_ms=new_latency,
                                    target_latency_ms=THRESHOLDS['latency_target'],
                                    prev_rps=metrics.get('throughput_rps', 0),
                                    new_rps=new_metrics.get('throughput_rps', 0)
                                )
                                
                                optimizer = get_optimizer()
                                prev_rps = metrics.get('throughput_rps', 0)
                                new_rps = new_metrics.get('throughput_rps', 0)
                                rps_ratio = (new_rps / prev_rps) if prev_rps > 0 else 1.0
                                
                                optimizer.record_experience(
                                    prev_rps=prev_rps,
                                    prev_latency_ms=current_latency,
                                    prev_seqs=metrics.get('current_max_num_seqs', 64),
                                    action_seqs=new_seqs,
                                    reward=reward,
                                    new_rps=new_rps,
                                    new_latency_ms=new_latency,
                                    new_seqs=new_seqs
                                )
                                logger.info(f"RL: reward={reward:.2f} | RPS: {prev_rps:.1f}→{new_rps:.1f} (x{rps_ratio:.2f}) | Latency: {current_latency:.1f}→{new_latency:.1f}ms")
                            except Exception as e:
                                logger.debug(f"RL recording failed: {e}")
                    else:
                        logger.warning(f"Config apply failed: {result}")
                else:
                    logger.info("No specific values provided, skipping")
            else:
                logger.info("No action needed")
            
            # Wait before next decision cycle
            await asyncio.sleep(DECISION_INTERVAL)
            
        except Exception as e:
            logger.error(f"Agent loop error: {e}")
            await asyncio.sleep(DECISION_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_agent_loop())
