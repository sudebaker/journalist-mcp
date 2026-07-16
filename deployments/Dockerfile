# Stage 1: Build MCP server
FROM golang:1.23-alpine AS server-builder
RUN apk add --no-cache git ca-certificates
WORKDIR /src/journalist-mcp
COPY . .
RUN go mod download && \
    CGO_ENABLED=0 go build -ldflags="-s -w" -trimpath \
    -o /out/bin/mcp-server ./cmd/server/

# Stage 2: Runtime
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir requests

COPY --from=server-builder /out/bin/mcp-server /app/bin/mcp-server
COPY tools/ /app/tools/
COPY configs/ /app/configs/

WORKDIR /app
ENV TOOLS_DIR=/app/tools
ENV TOOLS_DISCOVERY=manifest
ENV MCP_TOOLSET=osint-es

EXPOSE 8080
CMD ["/app/bin/mcp-server", "-config", "/app/configs/config.yaml"]
