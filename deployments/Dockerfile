# Stage 1: Build journalist-agent binaries (optimized: CGO_ENABLED=0, ldflags="-s -w", trimpath)
FROM golang:1.25-alpine AS agent-builder
RUN apk add --no-cache git ca-certificates
WORKDIR /src/journalist-agent
COPY journalist-agent/ .
RUN go mod download && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/boe-agent ./cmd/boe-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/borme-agent ./cmd/borme-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/bdns-agent ./cmd/bdns-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/ckan-agent ./cmd/ckan-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/cohesion-agent ./cmd/cohesion-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/contratacion-agent ./cmd/contratacion-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/datosgob-agent ./cmd/datosgob-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/fts-agent ./cmd/fts-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/pscp-agent ./cmd/pscp-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/ted-agent ./cmd/ted-agent/ && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/transparency-agent ./cmd/transparency-agent/

# Stage 2: Build MCP server (optimized)
FROM golang:1.23-alpine AS server-builder
RUN apk add --no-cache git ca-certificates
WORKDIR /src/journalist-mcp
COPY journalist-mcp/ .
RUN go mod download && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/mcp-server ./cmd/server/

# Stage 3: Runtime
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=agent-builder /out/bin/ /app/bin/
COPY --from=server-builder /out/bin/mcp-server /app/bin/
COPY journalist-mcp/tools/ /app/tools/
COPY journalist-mcp/configs/ /app/configs/

WORKDIR /app
ENV TOOLS_DIR=/app/tools
ENV TOOLS_DISCOVERY=manifest
ENV MCP_TOOLSET=osint-es

EXPOSE 8080
CMD ["/app/bin/mcp-server", "-config", "/app/configs/config.yaml"]
