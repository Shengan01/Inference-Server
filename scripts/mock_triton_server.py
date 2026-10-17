#!/usr/bin/env python3
"""
Lightweight Mock Triton Inference Server with Prometheus Metrics

Emulates Nvidia Triton Inference Server API and Prometheus metrics for:
- Testing in resource-constrained Kubernetes environments (Minikube / Kind / K3s)
- Running without physical NVIDIA GPU hardware
- Simulating realistic latency spikes under load and recovery upon reconfiguration
"""

import os
import sys
import time
import json
import random
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

class SimulationState:
    def __init__(self):
        self.lock = threading.Lock()
        self.max_num_seqs = 32  # Default conservative batch size
        self.gpu_memory_util = 0.5
        self.total_requests = 100
        self.failed_requests = 0
        self.total_duration_us = 500000.0
        self.total_queue_us = 50000.0
        self.active_requests = 0
        self.last_request_time = time.time()

    def record_request(self):
        with self.lock:
            self.active_requests += 1
            current_active = self.active_requests

        # Concurrency vs capacity determines latency:
        # If active requests exceed max_num_seqs, queuing occurs!
        load_ratio = current_active / max(1.0, float(self.max_num_seqs))
        if load_ratio <= 1.0:
            # Optimal processing
            latency_ms = 12.0 + (load_ratio * 10.0) + random.uniform(-2.0, 3.0)
            queue_ms = 1.0 + (load_ratio * 3.0)
        else:
            # Overloaded - queuing delay accumulates
            excess = load_ratio - 1.0
            latency_ms = 25.0 + (excess * 65.0) + random.uniform(-3.0, 5.0)
            queue_ms = 10.0 + (excess * 40.0)

        latency_ms = max(5.0, latency_ms)
        queue_ms = max(0.5, queue_ms)

        # Small processing delay
        time.sleep(min(0.04, latency_ms / 1000.0))

        with self.lock:
            self.total_requests += 1
            self.total_duration_us += (latency_ms * 1000.0)
            self.total_queue_us += (queue_ms * 1000.0)
            self.active_requests = max(0, self.active_requests - 1)

        return latency_ms

    def reconfigure(self, max_num_seqs, gpu_mem=None):
        with self.lock:
            self.max_num_seqs = max_num_seqs
            if gpu_mem:
                self.gpu_memory_util = gpu_mem
            print(f"[RECONFIGURE] Applied max_num_seqs={self.max_num_seqs}, gpu_memory={self.gpu_memory_util}")

    def get_metrics(self):
        with self.lock:
            load_factor = min(1.0, (self.active_requests + 1) / max(1, self.max_num_seqs))
            gpu_util = int(20 + (load_factor * 75))
            total_vram = 16384 * 1024 * 1024  # 16 GB in bytes
            used_vram = int(total_vram * min(0.95, self.gpu_memory_util + (load_factor * 0.15)))

            metrics_text = f"""# HELP nv_inference_request_success Number of successful inference requests
# TYPE nv_inference_request_success counter
nv_inference_request_success{{model="finance-qwen",version="1"}} {self.total_requests}

# HELP nv_inference_request_failure Number of failed inference requests
# TYPE nv_inference_request_failure counter
nv_inference_request_failure{{model="finance-qwen",version="1"}} {self.failed_requests}

# HELP nv_inference_request_duration_us Cumulative inference duration in microseconds
# TYPE nv_inference_request_duration_us counter
nv_inference_request_duration_us{{model="finance-qwen",version="1"}} {int(self.total_duration_us)}

# HELP nv_inference_queue_duration_us Cumulative queue wait duration in microseconds
# TYPE nv_inference_queue_duration_us counter
nv_inference_queue_duration_us{{model="finance-qwen",version="1"}} {int(self.total_queue_us)}

# HELP nv_gpu_utilization GPU Utilization percentage (0-100)
# TYPE nv_gpu_utilization gauge
nv_gpu_utilization{{gpu_uuid="GPU-mock-4080-k8s"}} {gpu_util}

# HELP nv_gpu_memory_used_bytes GPU Memory Used in Bytes
# TYPE nv_gpu_memory_used_bytes gauge
nv_gpu_memory_used_bytes{{gpu_uuid="GPU-mock-4080-k8s"}} {used_vram}

# HELP nv_gpu_memory_total_bytes GPU Memory Total in Bytes
# TYPE nv_gpu_memory_total_bytes gauge
nv_gpu_memory_total_bytes{{gpu_uuid="GPU-mock-4080-k8s"}} {total_vram}
"""
            return metrics_text

state = SimulationState()

class MockTritonHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Concise logging
        if "metrics" not in args[0]:
            sys.stdout.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {args[0]} {args[1]}\n")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/v2/health/ready", "/v2/health/live"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ready"}')

        elif path == "/metrics":
            metrics = state.get_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
            self.wfile.write(metrics.encode("utf-8"))

        elif path.startswith("/v2/models/finance-qwen"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "name": "finance-qwen",
                "versions": ["1"],
                "platform": "vllm",
                "state": "READY"
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/v2/models/finance-qwen"):
            # Inference endpoint
            latency = state.record_request()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "model_name": "finance-qwen",
                "model_version": "1",
                "outputs": [
                    {
                        "name": "text_output",
                        "data": ["Simulated financial market analysis response."]
                    }
                ],
                "mock_latency_ms": round(latency, 2)
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        elif "/load" in path or "/unload" in path:
            # Hot reload endpoint
            try:
                config_path = "/model_repository/finance-qwen/config.pbtxt"
                if os.path.exists(config_path):
                    with open(config_path, "r") as f:
                        content = f.read()
                        import re
                        match = re.search(r'key:\s*"max_num_seqs"\s*value:\s*\{\s*string_value:\s*"(\d+)"', content)
                        if match:
                            state.reconfigure(int(match.group(1)))
                else:
                    state.reconfigure(512)
            except Exception:
                state.reconfigure(512)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "reloaded"}')

        else:
            self.send_response(200)
            self.end_headers()

def run(port=8000):
    server_address = ('0.0.0.0', port)
    httpd = ThreadingHTTPServer(server_address, MockTritonHandler)
    print(f"Mock Triton Server (Threading) running on port {port}...")
    httpd.serve_forever()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    if port == 8000:
        def run_metrics_listener():
            metrics_server = ThreadingHTTPServer(('0.0.0.0', 8002), MockTritonHandler)
            metrics_server.serve_forever()
        t = threading.Thread(target=run_metrics_listener, daemon=True)
        t.start()
    run(port)
