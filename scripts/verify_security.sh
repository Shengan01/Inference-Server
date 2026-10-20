#!/usr/bin/env bash
set -e

NAMESPACE="inference-monitoring"

echo "=========================================================="
echo "AUDYT BEZPIECZEŃSTWA KUBERNETES DLA CI/CD"
echo "Namespace: $NAMESPACE"
echo "=========================================================="

echo ""
echo "[1] Weryfikacja RBAC (Zasada najmniejszych uprawnień - Least Privilege)..."
echo "Czy prometheus-sa może czytać pody? (Oczekiwane: yes):"
kubectl auth can-i get pods --as=system:serviceaccount:$NAMESPACE:prometheus-sa -n $NAMESPACE || true

echo "Czy prometheus-sa może tworzyć lub usuwać pody? (Oczekiwane: no):"
kubectl auth can-i delete pods --as=system:serviceaccount:$NAMESPACE:prometheus-sa -n $NAMESPACE || true

echo "Czy prometheus-sa może czytać Secrets? (Oczekiwane: no):"
kubectl auth can-i get secrets --as=system:serviceaccount:$NAMESPACE:prometheus-sa -n $NAMESPACE || true

echo ""
echo "[2] Weryfikacja SecurityContext w podach (Non-root, Capabilities drop)..."
kubectl get pods -n $NAMESPACE -o jsonpath='{range .items[*]}{"Pod: "}{.metadata.name}{"\n  NonRoot: "}{.spec.securityContext.runAsNonRoot}{"\n  User: "}{.spec.securityContext.runAsUser}{"\n"}{end}' || true

echo ""
echo "[3] Weryfikacja NetworkPolicies (Zero Trust)..."
kubectl get networkpolicies -n $NAMESPACE

echo ""
echo "[4] Weryfikacja ResourceQuota i LimitRange..."
kubectl get resourcequota -n $NAMESPACE
kubectl get limitrange -n $NAMESPACE

echo ""
echo "=========================================================="
echo "Audyt bezpieczeństwa zakończony."
echo "=========================================================="
