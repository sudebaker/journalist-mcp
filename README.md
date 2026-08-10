# journalist-mcp

Hybrid **Go + Python** Model Context Protocol (MCP) server for OSINT research on
Spanish public sources (BOE, BORME, TED, DOUE, contratación, transparencia…).

The Go core manages the MCP transport, a session, rate limiting, auth, and tool
discovery; each tool is a self-contained Python subprocess that speaks a strict
JSON contract over stdin/stdout.

## Highlights

- **Streamable HTTP transport** (`POST /mcp`, 2025 spec) plus health, metrics,
  upload, and OpenAPI endpoints.
- **42 OSINT tools** discovered from `tools/*/tool.yaml` manifests — no code
  changes to add or remove capabilities.
- **Per-client rate limiting** with secure `X-Forwarded-For` handling
  (honored only from configured trusted proxies).
- **Optional bearer auth** on `/mcp` and `/upload`.
- **Hardened by default**: no insecure default credentials, required secrets
  fail fast, data service ports bound to `127.0.0.1`.
- **Reproducible builds**: Go modules locked via `go.sum`, Python deps pinned in
  `deployments/requirements.txt`, Docker images pinned to verified tags.
- **Backup script** for consistent PostgreSQL dumps and data-volume archives.

## Architecture

```
┌─────────┐  POST /mcp (Streamable HTTP, bearer auth)
│ Clients │──────────────────────────────┐
└─────────┘                              ▼
                                 ┌─────────────────┐
                                 │  journalist-mcp │   (Go)
                                 │  - transport    │
                                 │  - orchestrator │
                                 │  - executor     │
                                 └────────┬────────┘
                              JSON stdin/stdout │  per-tool Python subprocess
                                 ┌────────────▼─────────────┐
                                 │ tools/<name>/main.py      │  (42 tools)
                                 └───────────────────────────┘
        external network                          internal network
      crawl4ai · searxng                    postgres · rustfs · memgraph
                                             opensearch · whisper · searxng
```

- **MCP contract**: each tool receives `{"request_id", "arguments"}` on stdin
  and writes a single JSON result (`{"success", "content", "structured_content",
  "error"}`) on stdout.
- **Tool discovery**: `tools_discovery: manifest` scans `tools/*/tool.yaml`
  (name, description, args schema, timeout). Config tools in `configs/*.yaml`
  can be appended or overridden (`tools_append`).
- **Deployment network model**: an isolated `internal` network (`internal: true`)
  holds data services; `journalist-mcp` is the only bridge to the public
  `external` network.

## HTTP endpoints

| Method | Path                 | Auth               | Description                            |
|--------|----------------------|--------------------|----------------------------------------|
| POST   | `/mcp`               | `MCP_API_KEY` (opt)| MCP Streamable HTTP endpoint           |
| POST   | `/upload`            | `MCP_UPLOAD_API_KEY` (opt) | File upload                    |
| GET    | `/health`            | –                  | Liveness probe                         |
| GET    | `/health/detailed`   | –                  | Dependency health reports              |
| GET    | `/metrics`           | –                  | Prometheus metrics                     |
| GET    | `/files/{tool}/{f}`  | –                  | Serve generated files                  |
| GET    | `/docs`, `/openapi.json` | –              | Swagger / OpenAPI                      |
| GET    | `/`                  | –                  | Service info + version                  |

An empty auth key disables protection (dev mode); a warning is logged.

## Get started

### Docker deployment (recommended)

```bash
cd deployments
cp .env.example .env          # then fill in real credentials (see below)
docker compose up -d --build
docker compose ps             # journalist-mcp listens on :8080
```

> The stack refuses to start without the required secrets (`${VAR:?...}`
> fail-fast) — see `deployments/.env.example`.

### Local development

```bash
# Go core
go build ./cmd/server/ && go test ./... -count=1
./server -config configs/config.yaml

# Python tools (offline-safe tests)
python3 -m pytest tests/common -q
python3 tests/tools/test_doue_search.py     # offline; live only with EUR-LEX creds
# Deterministic source-contract fixtures (no network)
python3 tests/tools/test_boe_search.py
python3 tests/tools/test_borme_search.py
python3 tests/tools/test_ted_search.py
```

## Configuration

`configs/config.yaml` is the reference config (env-expandable). Platform
configured via environment variables, most of them with safe defaults. Required
secrets (also enforced by `MCP_REQUIRE_SECRETS` at load time):

| Variable                 | Purpose                                   |
|--------------------------|-------------------------------------------|
| `DATABASE_URL`           | PostgreSQL DSN for `postgres` service     |
| `RUSTFS_ACCESS_KEY_ID`   | RustFS (S3) access key                    |
| `RUSTFS_SECRET_ACCESS_KEY` | RustFS (S3) secret key                  |
| `POSTGRES_PASSWORD`      | PostgreSQL password                       |
| `CRAWL4AI_TOKEN`         | crawl4ai API token                        |
| `SEARXNG_SECRET`         | SearXNG `secret_key`                      |

Optional / auth-related:

| Variable              | Purpose                                              |
|-----------------------|------------------------------------------------------|
| `MCP_API_KEY`         | Bearer token for `POST /mcp` (empty = unprotected)   |
| `MCP_UPLOAD_API_KEY`  | Bearer token for `POST /upload`                      |
| `MCP_REQUIRE_SECRETS` | `true` → fail at load if required secrets are absent |
| `BASE_URL`            | Public base URL for SSE/docs links                   |

Rate limiting and proxy trust live in config (`rate_limit_rps`,
`rate_limit_burst`, `trusted_proxies`). `trusted_proxies` is empty by default —
`X-Forwarded-For` is **ignored** unless the direct peer is in a listed CIDR.

## Security posture

- No default credentials ship anywhere (`internal/config/secrets.go` denylist
  + `ValidateSecrets` on load; CI scans the repo).
- `/mcp` and `/upload` can require `Bearer` tokens; optional for dev.
- Rate limiting honors `X-Forwarded-For` only from trusted proxy CIDRs.
- Compose binds data-service ports to `127.0.0.1` and isolates services in an
  internal network; required secrets fail fast.
- Images and dependencies are pinned; upgrades are deliberate (see
  `deployments/docker-compose.yml` header).

## CI (GitHub Actions)

`.github/workflows/ci.yml` runs on push/PR to `master`:

- **go** — `go vet`, `go test ./...`, build.
- **python** — installs pinned deps, runs offline tests (`tests/common`,
  offline DOUE smoke). Network-dependent runners run in a **non-blocking**
  `live-smoke` job.
- **compose** — asserts fail-fast without secrets and validates with a
  populated env.
- **docker-build** — builds the `journalist-mcp` image (validates pip + Go
  resolution).
- **secret-scan** — fails if known insecure default credentials appear outside
  the denylist source/tests.

## Source contract (`*_search` tools)

Every `*_search` tool returns a consistent `structured_content` envelope so the
orchestrator can aggregate, deduplicate and compare sources:

```json
{
  "source": "boe",
  "target": "B12345678",
  "results": [],
  "count": 0,
  "official": true,
  "evidence_type": "official_record"
}
```

Each item in `results` is an **Evidence** record built by
`tools/common/evidence.py`:

| Field           | Meaning                                                        |
|-----------------|----------------------------------------------------------------|
| `id`            | Deterministic `sha256(url|date|title)[:16]` — stable dedup key |
| `source`        | Source identifier                                              |
| `official`      | Trusted/official vs. generic (e.g. search engines)             |
| `confidence`    | 0.0–1.0                                                        |
| `title`, `date`, `url` | Core identifying fields                                |
| `entity`        | Referenced entity (NIF/CIF/name) — optional                    |
| `evidence_type` | Semantic type — optional                                       |
| `raw`           | Source-specific fields preserved verbatim — optional           |

Contract rules:

- `count == len(results)`; empty search is `success: true` with `results: []`.
- Optional fields are omitted rather than invented.
- `id` is stable across runs and sources, so the orchestrator can detect the
  same record published by two sources and count it once (`duplicates_removed`).

### Aggregation (`journalist_investigate`)

The orchestrator runs every `*_search` source in parallel and returns, per
source: `count`, `duration_ms`, `official`, `error_code`, and
`duplicates_removed`. `total_results` is the number of **unique records** (not
the number of sources that answered). A failing source never cancels the
others; each source keeps its configured order in the response.

### Adding a new search source

1. Create `tools/<name>_search/main.py` + `tool.yaml` following an existing
   source (e.g. `tools/ted_search`).
2. Emit evidence via `build_evidence(...)` and the envelope via
   `build_search_result(...)` from `tools/common/evidence.py`.
3. Map your source-specific fields into `raw`; keep `title`, `date`, `url`
   populated so `id` is meaningful.
4. Add deterministic fixtures in `tests/tools/test_<name>_search.py`
   (success, empty, HTTP error, timeout, changed-field tolerance) and register
   them in `.github/workflows/ci.yml`.
5. The orchestrator picks the tool up automatically (any tool whose name ends
   in `_search`); no Go change needed unless the tool needs special args.

## Backups

`scripts/backup.sh`:

- `pg_dump -Fc` of PostgreSQL (consistent, no downtime).
- Consistent tars of `rustfs`, `memgraph`, `opensearch` named volumes (services
  briefly stopped, then restarted via a `trap` even on failure).
- Writes to `backups/<timestamp>/`, prunes entries older than `KEEP_DAYS`
  (default 7). Override output with `BACKUP_DIR`.

```bash
scripts/backup.sh
BACKUP_DIR=/data/backups KEEP_DAYS=14 scripts/backup.sh
```

Restore:

```bash
# PostgreSQL (custom-format dump)
docker compose -f deployments/docker-compose.yml exec -T postgres \
  pg_restore -U mcp -d knowledge --clean --if-exists < backups/<ts>/postgres.dump

# Data volumes (example: rustfs)
docker compose -f deployments/docker-compose.yml stop rustfs
docker run --rm -v deployments_rustfs-data:/data -v "$PWD/backups/<ts>:/b:ro" \
  alpine:3.20 sh -c 'rm -rf /data/* && tar xzf /b/rustfs-data.tgz -C /data'
docker compose -f deployments/docker-compose.yml start rustfs
```

> The volume prefix `deployments_` is the default compose project name; check
> yours with `docker compose config --format json | python3 -c "import json,sys;print(json.load(sys.stdin)['name'])"`.

## Repository layout

```
cmd/server/        Go entrypoint (build metadata via ldflags)
internal/config/   Config load, validation, secret denylist
internal/transport/ HTTP/SSE/MCP servers, CORS, rate limit, auth
internal/executor/ Tool subprocess execution + env/args validation
tools/<name>/      Python tool + tool.yaml manifest (42 tools)
configs/           YAML configs (base / EN / local override)
deployments/       Dockerfile, docker-compose, .env.example, SearXNG settings
scripts/           backup.sh
tests/             Go + Python tests (offline gate, live smoke)
```