FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fix HIGH CVEs found by Trivy in transitive/base packages
RUN pip install --no-cache-dir --upgrade "wheel>=0.46.2" "jaraco.context>=6.1.0"

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
