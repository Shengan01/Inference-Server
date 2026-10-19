#!/usr/bin/env bash
# Script to maintain port-forwards to K8s services in the background

cleanup() {
    kill $(jobs -p) 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

kubectl port-forward -n inference-monitoring svc/triton-service 8000:8000 &
kubectl port-forward -n inference-monitoring svc/prometheus-service 9090:9090 &
kubectl port-forward -n inference-monitoring svc/mcp-agent-service 5000:5000 &
kubectl port-forward -n inference-monitoring svc/grafana-service 3000:3000 &

wait
