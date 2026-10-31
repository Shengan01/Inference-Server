#!/usr/bin/env bash
# Creates the secrets that are intentionally NOT stored in Git:
#   - grafana-secret : Grafana admin credentials (random password)
#   - inference-tls  : self-signed certificate for the Ingress host
#   - grafana-admin  : (only with OPERATOR_PROFILE=true) credentials for the
#                      kube-prometheus-stack Grafana, namespace "monitoring"
# Idempotent: existing secrets are left untouched.
set -euo pipefail

NAMESPACE="${NAMESPACE:-inference-monitoring}"
HOST="${INGRESS_HOST:-inference.local}"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

if kubectl -n "$NAMESPACE" get secret grafana-secret >/dev/null 2>&1; then
    echo "grafana-secret already exists - skipping"
else
    PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
    kubectl -n "$NAMESPACE" create secret generic grafana-secret \
        --from-literal=admin-user=admin \
        --from-literal=admin-password="$PASSWORD"
    echo "grafana-secret created. Read the password with:"
    echo "  kubectl -n $NAMESPACE get secret grafana-secret -o jsonpath='{.data.admin-password}' | base64 -d"
fi

if kubectl -n "$NAMESPACE" get secret inference-tls >/dev/null 2>&1; then
    echo "inference-tls already exists - skipping"
else
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
        -keyout "$TMP/tls.key" -out "$TMP/tls.crt" \
        -subj "/CN=$HOST" -addext "subjectAltName=DNS:$HOST" 2>/dev/null
    kubectl -n "$NAMESPACE" create secret tls inference-tls \
        --cert="$TMP/tls.crt" --key="$TMP/tls.key"
    echo "inference-tls created (self-signed for $HOST)"
fi

if [ "${OPERATOR_PROFILE:-false}" = "true" ]; then
    kubectl get namespace monitoring >/dev/null 2>&1 || kubectl create namespace monitoring
    if kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
        echo "grafana-admin already exists - skipping"
    else
        PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
        kubectl -n monitoring create secret generic grafana-admin \
            --from-literal=admin-user=admin \
            --from-literal=admin-password="$PASSWORD"
        echo "grafana-admin created in namespace monitoring"
    fi
fi
