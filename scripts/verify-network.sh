#!/usr/bin/env bash
# verify-network.sh — Verifica conectividad entre Ambassador y Workers.
# Correr desde la laptop principal ANTES de lanzar el coordinator.
#
# Uso: bash scripts/verify-network.sh

set -euo pipefail

# Cargar IPs desde .env si existe
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

AMBASSADOR_URL="${AMBASSADOR_URL:-http://localhost:5005}"
WORKER_1_URL="${WORKER_1_URL:-http://192.168.1.101:5001}"
WORKER_2_URL="${WORKER_2_URL:-http://192.168.1.102:5001}"
WORKER_3_URL="${WORKER_3_URL:-http://192.168.1.103:5001}"

PASS=0
FAIL=0

check() {
    local name="$1"
    local url="$2"
    local response
    if response=$(curl -sf --max-time 3 "$url/health" 2>/dev/null); then
        echo "✅  $name  →  $response"
        PASS=$((PASS + 1))
    else
        echo "❌  $name  →  SIN RESPUESTA en $url/health"
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "=== Verificación de conectividad ==="
echo ""
check "Ambassador" "$AMBASSADOR_URL"
check "Worker 1  " "$WORKER_1_URL"
check "Worker 2  " "$WORKER_2_URL"
check "Worker 3  " "$WORKER_3_URL"
echo ""
echo "=== Resultado: $PASS OK  /  $FAIL FALLO(S) ==="
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "⚠️  Revisa firewall en las laptops con fallo antes de continuar."
    exit 1
fi

echo "🟢  Todos los servicios responden. Puedes lanzar el coordinator."
