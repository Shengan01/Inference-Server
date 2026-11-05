#!/usr/bin/env bash
# Deploys the GPU profile (real Triton + vLLM on the host NVIDIA GPU) to a dedicated Minikube profile.
#
# Prerequisites: NVIDIA driver + nvidia-container-toolkit with the Docker runtime configured
#   sudo nvidia-ctk runtime configure --runtime=docker --set-as-default && sudo systemctl restart docker
# and the model files in models/finance-qwen/1/ (model.safetensors, config.json, tokenizer files, ...).
# Model files are not stored in the repository.
set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-gpu}"
MODEL_DIR="${MODEL_DIR:-models/finance-qwen/1}"
TRITON_IMAGE="nvcr.io/nvidia/tritonserver:24.06-vllm-python-py3"
NODE_MODEL_DIR="/mnt/model_repository/finance-qwen/1"

echo "== 1/7 Sprawdzanie GPU i Dockera"
nvidia-smi -L
docker info 2>/dev/null | grep -qi "runtimes:.*nvidia" || { echo "Docker nie ma runtime nvidia (patrz naglowek skryptu)"; exit 1; }
for f in model.safetensors config.json tokenizer.json; do
    [ -f "$MODEL_DIR/$f" ] || { echo "Brak $MODEL_DIR/$f - umiesc pliki modelu w $MODEL_DIR"; exit 1; }
done

echo "== 2/7 Klaster Minikube z GPU (profil: $PROFILE)"
if ! minikube status -p "$PROFILE" >/dev/null 2>&1; then
    # nvidia-container-toolkit < 1.14 crashes with NVIDIA_DRIVER_CAPABILITIES=all, which minikube passes
    # for --gpus; the shim asks for compute,utility instead (used only for this command).
    SHIM="$(mktemp -d)"
    trap 'rm -rf "$SHIM"' EXIT
    cat > "$SHIM/docker" <<'SH'
#!/usr/bin/env bash
args=()
for a in "$@"; do args+=("${a//NVIDIA_DRIVER_CAPABILITIES=all/NVIDIA_DRIVER_CAPABILITIES=compute,utility}"); done
exec "$(command -v -p docker || echo /usr/bin/docker)" "${args[@]}"
SH
    chmod +x "$SHIM/docker"
    PATH="$SHIM:$PATH" minikube start -p "$PROFILE" --driver=docker --container-runtime=docker --gpus all --cpus=8 --memory=16384
fi
minikube profile "$PROFILE" >/dev/null
minikube addons enable ingress >/dev/null || true
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=240s

echo "== 3/7 Model na wezle (hostPath /mnt/model_repository)"
minikube ssh -- "sudo mkdir -p $NODE_MODEL_DIR && sudo chmod -R 777 /mnt/model_repository"
# model.json and config.pbtxt are seeded from ConfigMaps by the init containers, so they are not copied
tar -C "$MODEL_DIR" --exclude=model.json --exclude=.cache -cf - . | docker exec -i "$PROFILE" tar -C "$NODE_MODEL_DIR" -xf -

echo "== 4/7 Obraz Triton + vLLM w wezle (duzy, pierwsze pobranie potrwa)"
minikube ssh -- "docker image inspect $TRITON_IMAGE >/dev/null 2>&1 || docker pull $TRITON_IMAGE"

echo "== 5/7 Obraz mcp-agent"
minikube image build -t mcp-agent:local ./mcp_agent

echo "== 6/7 Sekrety i manifesty (overlay local-gpu)"
./scripts/bootstrap_secrets.sh
kubectl apply -k k8s/overlays/local-gpu

echo "== 7/7 Oczekiwanie na gotowosc (Triton laduje model na GPU)"
kubectl -n inference-monitoring rollout status statefulset/prometheus --timeout=240s
for d in grafana alertmanager mcp-agent triton-server; do
    kubectl -n inference-monitoring rollout status "deployment/$d" --timeout=900s
done
kubectl -n inference-monitoring get pods -o wide
echo "Test: TEST_PROFILE=gpu LOADGEN_IMAGE=mcp-agent:local ./scripts/run_in_cluster_test.sh"
