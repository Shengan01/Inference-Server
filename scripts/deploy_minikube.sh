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
minikube image build -t mcp-agent:local ./mcp_agent || (eval $(minikube docker-env) && docker build -t mcp-agent:local ./mcp_agent)

# 4. Aplikacja manifestów za pomocą Kustomize
echo "Wdrażanie manifestów K8s w trybie: ${MODE}..."
# Secrets (Grafana admin password, TLS certificate) are not kept in Git.
./scripts/bootstrap_secrets.sh

# The local overlays use the image built above instead of the ghcr.io one.
if [ "$MODE" == "gpu" ]; then
    kubectl apply -k k8s/overlays/local-gpu
else
    kubectl apply -k k8s/overlays/local-cpu
fi

# 5. Oczekiwanie na gotowość Podów
echo "Oczekiwanie na gotowość podów w namespace 'inference-monitoring'..."
kubectl rollout status statefulset/prometheus -n inference-monitoring --timeout=180s || true
kubectl rollout status deployment/grafana -n inference-monitoring --timeout=180s || true
kubectl rollout status deployment/alertmanager -n inference-monitoring --timeout=180s || true
kubectl rollout status deployment/mcp-agent -n inference-monitoring --timeout=180s || true
kubectl rollout status deployment/triton-server -n inference-monitoring --timeout=180s || true

echo ""
echo "=========================================================="
echo "Wdrożenie zakończone."
echo "=========================================================="
echo "Status podów:"
kubectl get pods -n inference-monitoring -o wide
echo ""
echo "Dostęp do usług (Ingress, wymaga wpisu w /etc/hosts):"
echo "  echo \"\$(minikube ip) inference.local\" | sudo tee -a /etc/hosts"
echo "  (alternatywnie: 'minikube tunnel' w osobnym terminalu dla typu LoadBalancer)"
echo "Grafana:    https://inference.local/grafana/   (login: admin)"
echo "            Hasło: kubectl -n inference-monitoring get secret grafana-secret -o jsonpath='{.data.admin-password}' | base64 -d"
echo "Triton:     https://inference.local/v2/health/ready"
echo "MCP Agent:  https://inference.local/mcp/health"
echo "Prometheus / Alertmanager (bez Ingress, bez uwierzytelniania): ./scripts/start_port_forward.sh"
echo "=========================================================="
