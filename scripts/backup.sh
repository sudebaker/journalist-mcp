#!/usr/bin/env bash
#
# backup.sh — Backup del stack journalist-mcp (docker compose).
#
# Qué se respalda:
#   ┌ postgres  → pg_dump lógico (consistent, en caliente) con formato -Fc.
#   └ rustfs, memgraph, opensearch → tar de su volumen nombrado con el
#     servicio detenido (backup consistente). Se reinician con un trap.
#
# Uso:
#   scripts/backup.sh                 # backup completo a ./backups/<fecha>/
#   BACKUP_DIR=/data/backups scripts/backup.sh
#   KEEP_DAYS=14 scripts/backup.sh    # conserva 14 días (por defecto 7)
#
# Requisitos: docker compose v2 y el archivo deployments/.env con credenciales.
# El nombre del proyecto y los volúmenes se derivan de `docker compose config`,
# así que la ruta de despliegue puede variar sin romper el script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deployments/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/deployments/.env}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
KEEP_DAYS="${KEEP_DAYS:-7}"
# Ocu-investigacion activa memgraph/opensearch, que viven tras el profile.
COMPOSE_PROFILE="${COMPOSE_PROFILE:-ocu-investigacion}"

# Servicios cuyos datos viven en volúmenes y requieren parada consistente.
# Usamos las CLAVES de servicio (no los container_name).
DATA_SERVICES=(rustfs memgraph opensearch)
DATA_VOLUMES=(rustfs-data memgraph-data opensearch-data)

log()  { printf '[backup] %s\n' "$*"; }
die()  { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

if [[ ! -f "$COMPOSE_FILE" ]]; then
  die "compose file no encontrado: $COMPOSE_FILE"
fi
command -v docker >/dev/null || die "docker no está instalado"

# Cargar credenciales de deployments/.env si existe (no es obligatorio).
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

POSTGRES_USER="${POSTGRES_USER:-mcp}"
POSTGRES_DB="${POSTGRES_DB:-knowledge}"

# Recupera el nombre del proyecto compose (p. ej. "deployments") y los volúmenes
# del stack. Los volúmenes se materializan como "<proyecto>_<nombre>".
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$BACKUP_DIR/$STAMP"
COMPOSE_JSON="$(docker compose --profile "$COMPOSE_PROFILE" -f "$COMPOSE_FILE" config --format json)"
PROJECT="$(printf '%s' "$COMPOSE_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["name"])')"

mapfile -t STACK_VOLUMES < <(
  printf '%s' "$COMPOSE_JSON" | python3 -c 'import json,sys;print("\n".join(json.load(sys.stdin)["volumes"].keys()))'
)

mkdir -p "$OUT_DIR"
log "proyecto: $PROJECT   destino: $OUT_DIR"
log "volúmenes del stack: ${STACK_VOLUMES[*]:-ninguno}"

# Verifica que los volúmenes que vamos a tar existan (evita crear volúmenes vacíos).
for vol in "${DATA_VOLUMES[@]}"; do
  docker volume inspect "${PROJECT}_${vol}" >/dev/null 2>&1 \
    || die "volumen ${PROJECT}_${vol} no existe — ¿está levantado el stack?"
done

# Dumpeo lógico de PostgreSQL (consistente sin parar el servicio).
log "PostgreSQL: pg_dump de $POSTGRES_DB ($POSTGRES_USER)…"
docker compose --profile "$COMPOSE_PROFILE" -f "$COMPOSE_FILE" exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
  > "$OUT_DIR/postgres.dump" \
  || die "pg_dump falló"

restart_data_services() {
  docker compose --profile "$COMPOSE_PROFILE" -f "$COMPOSE_FILE" start "${DATA_SERVICES[@]}" >/dev/null \
    || log "AVISO: no se pudo reiniciar ${DATA_SERVICES[*]} — revísalo manualmente"
  log "servicios de datos reiniciados"
}

cleanup() {
  local rc=$?
  if (( rc != 0 )); then
    log "error durante el backup (rc=$rc) — reiniciando servicios de datos"
    restart_data_services
  fi
}
trap cleanup EXIT

# Volúmenes con el servicio detenido → tar gzip consistente.
log "deteniendo ${DATA_SERVICES[*]} para backup consistente…"
docker compose --profile "$COMPOSE_PROFILE" -f "$COMPOSE_FILE" stop "${DATA_SERVICES[@]}" >/dev/null || \
  die "no se pudo detener ${DATA_SERVICES[*]}"

for vol in "${DATA_VOLUMES[@]}"; do
  log "tar del volumen ${PROJECT}_${vol}…"
  docker run --rm -v "${PROJECT}_${vol}:/data:ro" alpine:3.20 \
    tar czf - -C /data . > "$OUT_DIR/$vol.tgz" \
    || die "tar del volumen $vol falló"
done

restart_data_services

# Poda de backups antiguos (solo archivos generados por este script).
log "limpiando backups con más de $KEEP_DAYS días…"
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' -mtime "+$KEEP_DAYS" \
  -exec rm -rf {} +
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type f -name '*.tgz' -mtime "+$KEEP_DAYS" -delete

log "backup completado:"
du -sh "$OUT_DIR"/* 2>/dev/null | sed 's/^/  /'
log "OK"