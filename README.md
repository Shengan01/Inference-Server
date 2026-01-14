# 🚀 Autonomic Inference Server

**Self-Healing AI Infrastructure with LLM-Powered Decision Making**

An inference server that uses an LLM to autonomously optimize its own configuration in real-time — acting like a digital Site Reliability Engineer.

![Architecture](https://img.shields.io/badge/Architecture-4%20Containers-blue)
![GPU](https://img.shields.io/badge/GPU-NVIDIA%20RTX-green)
![Framework](https://img.shields.io/badge/Inference-Triton%20+%20vLLM-orange)

---

## 🎯 What It Does

When traffic spikes cause latency degradation:
1. **MCP Agent** detects the issue via Prometheus metrics
2. **LLM Agent** reasons about optimal configuration (Qwen via Ollama)
3. **Agent reconfigures** Triton dynamically without human intervention
4. **Feedback loop** validates changes and rolls back if needed
5. **RL module** learns from outcomes to improve future decisions

## 📦 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       Docker Network                         │
│                                                             │
│  ┌───────────┐   ┌───────────┐   ┌───────────┐             │
│  │  Triton   │◄──│Prometheus │◄──│MCP Agent  │             │
│  │  Server   │   │ (Metrics) │   │ (Brain)   │             │
│  │           │   │           │   │           │             │
│  │  • vLLM   │   │ • Scrape  │   │ • Tools   │             │
│  │  • GPTQ   │   │ • Store   │   │ • APIs    │             │
│  └───────────┘   └───────────┘   └─────┬─────┘             │
│        ▲                               │                    │
│        │                               ▼                    │
│  ┌───────────┐   ┌───────────┐   ┌───────────┐             │
│  │  Grafana  │   │ LLM Agent │◄──│  Ollama   │             │
│  │(Dashboard)│   │ (Thinker) │   │ (Qwen2.5) │             │
│  └───────────┘   └───────────┘   └───────────┘             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

**Prerequisites:** NVIDIA GPU, Docker with nvidia-docker2, Ollama running locally

```bash
# 1. Start all services (including LLM agent)
docker compose up -d

# 2. Run the demo
python simulation.py
```

## 📁 Project Structure

```
├── docker-compose.yml      # Container orchestration
├── simulation.py           # Load generation demo
│
├── mcp_agent/              # MCP Server (The Brain)
│   ├── server.py           # FastMCP with tools
│   └── tools/
│       ├── health.py       # Prometheus queries
│       └── reconfigure.py  # Triton hot reload
│
├── llm_agent/              # Autonomous Agent
│   ├── agent.py            # Decision loop
│   └── learning.py         # Q-learning RL
│
├── models/                 # Model repository
├── training/               # Fine-tuning scripts
├── prometheus/             # Metrics config
└── grafana/                # Dashboards
```

## 🔑 Key Features

| Feature | Description |
|---------|-------------|
| **Autonomous Decisions** | LLM reasons about optimal config |
| **Full Autonomy** | LLM has complete control, no rule overrides |
| **Feedback Loop** | Rolls back if latency degrades |
| **RL Learning** | Q-learning improves over time |
| **Hot Reload** | Reconfigure without restart |
| **GPU Safety** | Checks memory before reload |

## 📈 Tech Stack

| Layer | Technology |
|-------|------------|
| Model | Qwen2-0.5B (GPTQ quantized) |
| Training | Unsloth + QLoRA |
| Serving | Nvidia Triton + vLLM |
| Agent Protocol | FastMCP |
| Learning | Q-Learning |
| Monitoring | Prometheus + Grafana |

## 📊 Demo Phases

The simulation runs through 8 phases:

| Phase | RPS | Expected Agent Behavior |
|-------|-----|------------------------|
| IDLE | 0 | Scale down to min |
| LIGHT | 50 | Small config |
| MEDIUM | 500 | Medium config |
| HEAVY | 2000 | Large config |
| EXTREME | 5000 | Maximum settings |
| SUSTAINED | 3000 | Maintain high |
| RECOVERY | 500 | Scale back |
| IDLE | 5 | Return to minimum |

## 🐛 Troubleshooting

```bash
# Check Triton
docker compose logs triton_server

# Watch agent decisions
docker logs llm_agent -f

# Check services
docker compose ps
```

---

*Portfolio project demonstrating AI infrastructure and autonomous systems.*
