#!/usr/bin/env bash
# Runs the load / alert / hot-reload test procedure INSIDE the cluster as a
# Job, so results are not distorted by kubectl port-forward overhead.
# Writes test-output.log and test-report.json to $OUT_DIR (default: .).
set -euo pipefail

# TEST_PROFILE=gpu selects the settings for a real Triton + vLLM engine (GPU profile): requests use the
# generate endpoint, latency is the in-cluster client latency (the decoupled vLLM model does not report
# generation time in nv_inference_request_duration_us), and the engine starts from a conservative
# max_num_seqs=4 so that the batch capacity is the bottleneck during the load spike.
if [ "${TEST_PROFILE:-}" = "gpu" ]; then
    : "${INFER_MODE:=generate}" "${LATENCY_SOURCE:=client}" "${RESET_SEQS:=4}" "${RESET_GPU_MEMORY:=0.4}"
    : "${GEN_MAX_TOKENS:=64}" "${CALM_RPS:=4}" "${STORM_RPS:=60}" "${RECOVERED_MAX_MS:=2000}"
    export INFER_MODE LATENCY_SOURCE RESET_SEQS RESET_GPU_MEMORY GEN_MAX_TOKENS CALM_RPS STORM_RPS RECOVERED_MAX_MS
fi

NAMESPACE="${NAMESPACE:-inference-monitoring}"
IMAGE="${LOADGEN_IMAGE:-mcp-agent:local}"
OUT_DIR="${OUT_DIR:-.}"
mkdir -p "$OUT_DIR"

kubectl apply -f k8s/tests/loadgen-networkpolicy.yaml
kubectl -n "$NAMESPACE" create configmap loadgen-script \
    --from-file=test_k8s_simulation.py=scripts/test_k8s_simulation.py \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NAMESPACE" delete job loadgen --ignore-not-found --wait=true
# Defaults suit the mock Triton; for a real Triton (GPU profile) use:
#   INFER_MODE=generate LATENCY_SOURCE=client RESET_SEQS=32 GEN_MAX_TOKENS=128 CALM_RPS=8 \
#   RECOVERED_MAX_MS=<bound> STORM_RPS=<burst>
sed -e "s#__IMAGE__#${IMAGE}#" \
    -e "s#__INFER_MODE__#${INFER_MODE:-infer}#" \
    -e "s#__RESET_SEQS__#${RESET_SEQS:-0}#" \
    -e "s#__RESET_GPU_MEMORY__#${RESET_GPU_MEMORY:-0.4}#" \
    -e "s#__RECOVERED_MAX_MS__#${RECOVERED_MAX_MS:-30}#" \
    -e "s#__STORM_RPS__#${STORM_RPS:-400}#" \
    -e "s#__LATENCY_SOURCE__#${LATENCY_SOURCE:-prometheus}#" \
    -e "s#__GEN_MAX_TOKENS__#${GEN_MAX_TOKENS:-16}#" \
    -e "s#__CALM_RPS__#${CALM_RPS:-20}#" \
    k8s/tests/loadgen-job.yaml | kubectl apply -f -

# Wait until the Job finished (succeeded or failed)
for _ in $(seq 1 180); do
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
