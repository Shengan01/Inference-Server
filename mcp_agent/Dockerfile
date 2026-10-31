FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build tooling is not needed at runtime; setuptools/wheel vendor vulnerable
# copies of jaraco.context and wheel (HIGH CVEs reported by Trivy).
RUN pip uninstall -y setuptools wheel

# Copy application code
COPY . .

# Expose MCP server port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

USER 10001

# Run the MCP server
CMD ["python", "server.py"]
