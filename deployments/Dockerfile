# Stage 1: Build MCP server (Go)
FROM golang:1.23-alpine AS server-builder
RUN apk add --no-cache git ca-certificates
WORKDIR /src/journalist-mcp
COPY . .
RUN go mod download && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/mcp-server ./cmd/server/

# Stage 2: Python base with system dependencies
FROM python:3.12-slim AS python-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    # WeasyPrint (PDF generation)
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    # For OpenCV / image tools
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    # For PostgreSQL (psycopg2)
    libpq-dev \
    # For python-magic
    libmagic1 \
    # General
    curl \
    && rm -rf /var/lib/apt/lists/*

# Stage 3: Install Python dependencies
FROM python-base AS python-deps

WORKDIR /app

COPY <<EOF /app/requirements.txt
# Core
requests>=2.31.0

# Web scraping
beautifulsoup4>=4.12.0

# PDF Reports
jinja2>=3.1.2
weasyprint>=60.0
markdown>=3.5

# Data
numpy>=1.24.0
pillow>=10.0.0

# PostgreSQL
psycopg2-binary>=2.9.0

# S3 (RustFS/MinIO)
minio>=7.2.0

# Forensic investigation tools
rapidfuzz>=3.0.0
python-magic>=0.4.27
imagehash>=4.3.1
folium>=0.14.0

# Graph / search drivers
neo4j>=5.14.0
opensearch-py>=2.4.0

# Document parsing
pypdf>=3.0.0
python-docx>=1.0.0

# Config
pyyaml>=6.0
EOF

RUN pip install --no-cache-dir -r requirements.txt

# Stage 4: Final image
FROM python-base AS final

WORKDIR /app

# Copy Python packages from deps stage
COPY --from=python-deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=python-deps /usr/local/bin /usr/local/bin

# Copy Go binary
COPY --from=server-builder /out/bin/mcp-server /app/bin/mcp-server

# Copy configuration, tools and templates
COPY tools/ /app/tools/
COPY configs/ /app/configs/
COPY templates/ /app/templates/

# Create data directory
RUN mkdir -p /data/reports

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TOOLS_DIR=/app/tools
ENV TOOLS_DISCOVERY=manifest
ENV MCP_TOOLSET=osint-es

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["/app/bin/mcp-server", "-config", "/app/configs/config.yaml"]