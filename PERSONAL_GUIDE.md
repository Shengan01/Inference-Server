# 🧠 Personal Project Guide: Autonomic Inference Server

This is your personal reference for understanding and explaining this project in interviews.

---

## 🎯 The Core Idea (Elevator Pitch)

> "Instead of scaling out with more servers when traffic spikes, this system uses an LLM to intelligently tune the software configuration of existing hardware in real-time, like a digital SRE."

**Key differentiators:**
- LLM makes decisions autonomously (not just rule-based)
- Feedback loop validates changes and rolls back if things get worse
- Q-learning improves decisions over time

---

## 📁 File-by-File Code Explanation

### `simulation.py` — The Demo Script

**What it does:** Generates load in 8 phases and shows a live dashboard.

**Key sections:**
```python
# Phases simulate real traffic patterns
phases = [
    ("PHASE 1: IDLE", 0, 15),        # Agent should scale DOWN
    ("PHASE 2: LIGHT LOAD", 50, 30),  # Small config
    ...
    ("PHASE 5: EXTREME SPIKE", 5000, 45),  # Max settings needed
]
```

**How load is generated:**
```python
async def fire_request(client, prompt):
    success, latency = await send_request(client, prompt)
    metrics.latency_history.append(latency)  # Rolling average
```

**Interview point:** The demo uses `asyncio.create_task()` for fire-and-forget requests, tracking metrics without blocking.

---

### `mcp_agent/server.py` — The MCP Server

**What it does:** Exposes tools that the LLM can call via Model Context Protocol.

**Key concept - MCP Tools:**
```python
@mcp.tool(name="get_health_status", description="...")
async def tool_get_health_status() -> dict:
    return await get_health_status(PROMETHEUS_URL, MODEL_NAME)

@mcp.tool(name="reconfigure_server", description="...")  
async def tool_reconfigure_server(strategy: str) -> dict:
    # Maps strategy names to max_num_seqs values
    strategy_map = {"aggressive": 512, "latency_optimized": 8, "balanced": 64}
```

**Interview point:** MCP is a standard protocol for LLMs to interact with tools. FastMCP is the Python implementation.

---

### `mcp_agent/tools/health.py` — Prometheus Queries

**What it does:** Queries Prometheus for real-time metrics.

**Key Prometheus queries:**
```python
# Average latency per request (15-second window)
query = f'rate(nv_inference_request_duration_us{{model="{model_name}"}}[15s]) / rate(nv_inference_request_success{{model="{model_name}"}}[15s])'

# Throughput (requests per second)
query = f'rate(nv_inference_request_success{{model="{model_name}"}}[15s])'
```

**Status calculation:**
```python
def _calculate_status(avg_latency_ms, gpu_utilization):
    if avg_latency_ms >= 50 or gpu_utilization >= 95:
        return "CRITICAL"
    if avg_latency_ms >= 30 or gpu_utilization >= 80:
        return "WARNING"
    return "HEALTHY"
```

---

### `mcp_agent/tools/reconfigure.py` — Hot Reload Logic

**What it does:** Modifies Triton's config file and triggers model reload.

**Key safety feature - GPU memory check:**
```python
async def check_gpu_memory_safe():
    query = 'nv_gpu_memory_used_bytes / nv_gpu_memory_total_bytes'
    # Only reload if GPU memory < 80%
    is_safe = utilization < 0.80
    return is_safe, utilization
```

**Config modification (regex):**
```python
def _update_config(config_content, max_num_seqs, gpu_memory):
    pattern_seqs = r'(key:\s*"max_num_seqs"\s*value:\s*\{\s*string_value:\s*)"[^"]*"'
    config_content = re.sub(pattern_seqs, f'\\g<1>"{max_num_seqs}"', config_content)
```

**Hot reload sequence:**
```python
# 1. Unload model
await client.post(f"{triton_url}/v2/repository/models/{model_name}/unload")
# 2. Wait for cleanup
await asyncio.sleep(1)
# 3. Reload with new config
await client.post(f"{triton_url}/v2/repository/models/{model_name}/load")
```

---

### `llm_agent/agent.py` — The Autonomous Brain ⭐

**This is the crown jewel.** Understand this well.

**Main loop structure:**
```python
while True:
    # 1. OBSERVE
    metrics = await get_metrics()
    
    # 2. REASON (ask LLM - FULLY AUTONOMOUS)
    decision = await ask_llm(metrics)  # No overrides!
    
    # 3. ACT (apply config)
    if decision["action"] == "reconfigure":
        await apply_config(new_seqs, new_gpu_memory)
    
    # 4. VALIDATE (feedback loop - rollback if worse)
    await validate_and_maybe_rollback(...)
    
    # 5. LEARN (RL)
    optimizer.record_experience(...)
```

**How it asks the LLM:**
```python
async def ask_llm(metrics):
    user_prompt = f"""Current server state:
    - Average Latency: {latency:.1f} ms [{latency_state}]
    - Throughput: {rps:.1f} requests/second [{load_state}]
    ...
    Based on these metrics, what configuration changes should be made?
    Respond with JSON only."""
    
    response = await client.post(f"{OLLAMA_URL}/api/generate", json={
        "model": OLLAMA_MODEL,
        "prompt": user_prompt,
        "system": SYSTEM_PROMPT,
        "format": "json"
    })
```

**The LLM is FULLY AUTONOMOUS** - it makes all configuration decisions based on its system prompt. The only safety mechanism is the feedback loop that validates outcomes.

**Feedback loop (rollback):**
```python
async def validate_and_maybe_rollback(agent_state, previous_latency):
    await asyncio.sleep(3)  # Wait for config to take effect
    new_metrics = await get_metrics()
    
    degradation_ratio = new_latency / previous_latency
    if degradation_ratio > 1.5:  # 50% worse
        # ROLLBACK
        await apply_config(previous_config.max_num_seqs, previous_config.gpu_memory)
```

---

### `llm_agent/learning.py` — Q-Learning Module

**What it does:** Learns which configurations work best over time.

**State discretization:**
```python
RPS_BINS = [0, 5, 20, 100, 500, 1000, float('inf')]
LATENCY_BINS = [0, 3, 5, 8, 10, 20, float('inf')]
ACTIONS = [8, 16, 32, 64, 128, 256, 512, 1024]  # max_num_seqs options
```

**Reward function:**
```python
def compute_reward(prev_latency_ms, new_latency_ms, target_latency_ms, rps):
    # Improvement ratio
    improvement = (prev_latency_ms - new_latency_ms) / prev_latency_ms
    latency_reward = improvement * 10
    
    # Bonus for meeting target
    if new_latency_ms <= target_latency_ms:
        target_bonus = 2.0
    
    # Bonus for efficiency when idle
    if rps < 5 and new_latency_ms < target_latency_ms:
        efficiency_bonus = 1.0
```

**Q-learning update:**
```python
def _update_q_value(self, exp):
    current_q = self.q_table[state_key][action_idx]
    td_target = reward + 0.95 * max(next_q_values)  # discount factor
    new_q = current_q + 0.1 * (td_target - current_q)  # learning rate
```

---

### `training/train_qlora.py` — Model Fine-Tuning

**What it does:** Fine-tunes Qwen2 on financial sentiment data.

**Key concepts:**
- **QLoRA**: Quantized Low-Rank Adaptation - trains only small adapters, not full model
- **Unsloth**: Library that makes training 2x faster

**LoRA config:**
```python
model = FastLanguageModel.get_peft_model(
    model,
    r=16,  # Rank (size of LoRA matrices)
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", ...],
    lora_alpha=16,
)
```

---

## 🔑 Key Interview Points

### 1. "Why not just use rule-based scaling?"
> "Rule-based systems are rigid. An LLM can understand nuanced situations like 'latency is high but trending down' or 'this is a weekend pattern'. Plus, the RL module learns from outcomes, so it gets smarter over time."

### 2. "How do you prevent the LLM from making bad decisions?"
> "The LLM is fully autonomous with a clear system prompt that defines the decision logic. But we have a feedback loop: after every config change, we measure if latency improved. If it degraded by more than 50%, we automatically roll back. This validates outcomes rather than pre-empting decisions, allowing the LLM to learn and adapt."

### 3. "What's MCP?"
> "Model Context Protocol - it's a standard way for LLMs to interact with external tools. Think of it like giving the LLM 'API endpoints' it can call. FastMCP is the Python implementation."

### 4. "Why Triton + vLLM?"
> "Triton is Nvidia's production inference server with dynamic batching. vLLM uses PagedAttention for memory-efficient serving. Together they give high throughput with hot-reload capability."

### 5. "What's the Q-learning doing?"
> "It discretizes the state (RPS bin + latency bin + current config) and learns which max_num_seqs value gives the best reward (latency improvement). Over time, it can suggest actions without needing the LLM."

---

## 🎬 Demo Script

When showing the project:

1. **Start services:** `docker compose up -d` (includes LLM agent)
2. **Run demo:** `python simulation.py`
3. **Watch agent logs:** `docker logs llm_agent -f` (in another terminal)

Point out:
- When IDLE → agent scales down to max_num_seqs=8
- When EXTREME → agent scales up to max_num_seqs=1024
- The LLM's reasoning appears in the logs

---

## 📊 Metrics to Know

| Metric | What It Measures | Good Range |
|--------|------------------|------------|
| `max_num_seqs` | Parallel sequences Triton can handle | 8-1024 |
| `gpu_memory_utilization` | % of VRAM to use | 0.3-0.9 |
| Latency | Time per request | <10ms |
| RPS | Requests per second | depends on load |

---

Good luck with interviews! 🚀
