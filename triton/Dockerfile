# Triton Inference Server with vLLM Backend
FROM nvcr.io/nvidia/tritonserver:24.06-vllm-python-py3

# Set working directory
WORKDIR /opt/tritonserver

# Expose ports
# 8000 - HTTP inference
# 8001 - gRPC inference  
# 8002 - Metrics (Prometheus)
EXPOSE 8000 8001 8002

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD curl -f http://localhost:8000/v2/health/ready || exit 1

# Start Triton with model repository
CMD ["tritonserver", \
     "--model-repository=/model_repository", \
     "--model-control-mode=explicit", \
     "--load-model=finance-qwen", \
     "--http-port=8000", \
     "--grpc-port=8001", \
     "--metrics-port=8002", \
     "--log-verbose=1"]
