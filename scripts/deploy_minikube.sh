#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "Wdrażanie Klastra Monitoringu Inferencji na Minikube"
echo "=========================================================="

MODE=${1:-"cpu-demo"}

# 1. Sprawdzenie Minikube
if ! minikube status >/dev/null 2>&1; then
    echo "Minikube nie jest uruchomiony. Startowanie minikube..."
    minikube start --driver=docker --cpus=4 --memory=6144
fi

# 2. Włączenie niezbędnych add-onów
echo "Włączanie addonów: ingress, metrics-server..."
minikube addons enable ingress || true
minikube addons enable metrics-server || true

# 3. Budowanie obrazu MCP Agenta wewnątrz środowiska Dockera Minikube
echo "Budowanie obrazu mcp-agent w Minikube..."
minikube image build -t mcp-agent:latest ./mcp_agent || (eval $(minikube docker-env) && docker build -t mcp-agent:latest ./mcp_agent)

# 4. Aplikacja manifestów za pomocą Kustomize
echo "Wdrażanie manifestów K8s w trybie: ${MODE}..."
if [ "$MODE" == "gpu" ]; then
    kubectl apply -k k8s/overlays/gpu
else
    kubectl apply -k k8s/overlays/cpu-demo
fi

# 5. Oczekiwanie na gotowość Podów
echo "Oczekiwanie na gotowość podów w namespace 'inference-monitoring'..."
kubectl rollout status statefulset/prometheus -n inference-monitoring --timeout=180s || true
kubectl rollout status deployment/grafana -n inference-monitoring --timeout=180s || true
kubectl rollout status deployment/mcp-agent -n inference-monitoring --timeout=180s || true
kubectl rollout status deployment/triton-server -n inference-monitoring --timeout=180s || true

echo ""
echo "=========================================================="
echo "Wdrożenie zakończone."
echo "=========================================================="
echo "Status podów:"
kubectl get pods -n inference-monitoring -o wide
echo ""
echo "Dostęp do usług:"
echo "Grafana:    http://localhost:3000 (lub kubectl port-forward -n inference-monitoring svc/grafana-service 3000:3000)"
echo "            Login: admin / Hasło: admin_k8s_secure_2026!"
echo "Prometheus: kubectl port-forward -n inference-monitoring svc/prometheus-service 9090:9090"
echo "Triton:     kubectl port-forward -n inference-monitoring svc/triton-service 8000:8000"
echo "MCP Agent:  kubectl port-forward -n inference-monitoring svc/mcp-agent-service 5000:5000"
echo "=========================================================="
