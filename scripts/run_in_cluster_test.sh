#!/usr/bin/env bash
# Runs the load / alert / hot-reload test procedure INSIDE the cluster as a
# Job, so results are not distorted by kubectl port-forward overhead.
# Writes test-output.log and test-report.json to $OUT_DIR (default: .).
set -euo pipefail

NAMESPACE="${NAMESPACE:-inference-monitoring}"
IMAGE="${LOADGEN_IMAGE:-mcp-agent:local}"
OUT_DIR="${OUT_DIR:-.}"
mkdir -p "$OUT_DIR"

kubectl apply -f k8s/tests/loadgen-networkpolicy.yaml
kubectl -n "$NAMESPACE" create configmap loadgen-script \
    --from-file=test_k8s_simulation.py=scripts/test_k8s_simulation.py \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NAMESPACE" delete job loadgen --ignore-not-found --wait=true
sed "s#__IMAGE__#${IMAGE}#" k8s/tests/loadgen-job.yaml | kubectl apply -f -

# Wait until the Job finished (succeeded or failed)
for _ in $(seq 1 90); do
    ok="$(kubectl -n "$NAMESPACE" get job loadgen -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
    bad="$(kubectl -n "$NAMESPACE" get job loadgen -o jsonpath='{.status.failed}' 2>/dev/null || true)"
    if [ -n "$ok" ] || [ -n "$bad" ]; then break; fi
    sleep 5
done

kubectl -n "$NAMESPACE" logs job/loadgen | tee "$OUT_DIR/test-output.log"
grep '^REPORT_JSON ' "$OUT_DIR/test-output.log" | sed 's/^REPORT_JSON //' > "$OUT_DIR/test-report.json" || true

if [ -z "${ok:-}" ]; then
    echo "Test procedure FAILED (Job did not succeed)"
    exit 1
fi
echo "Test procedure PASSED"
