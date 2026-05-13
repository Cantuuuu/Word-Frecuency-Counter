# Containerización Multi-Máquina — 1 Main + 3 Workers

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distribuir el sistema en 4 laptops conectadas por WiFi — 1 laptop principal (Ambassador + Coordinator) y 3 laptops worker — cada una leyendo su copia local de `wiki_es.txt`.

**Architecture:** La laptop principal levanta el Ambassador como servicio persistente y lanza el Coordinator manualmente una vez que los 3 workers estén listos. Cada laptop worker corre un único contenedor Worker que lee su copia local del archivo por bind-mount. La comunicación va Ambassador→Workers (el Ambassador hace POST a cada worker; los workers nunca inician conexiones).

**Tech Stack:** Docker, Docker Compose v2, Python 3.11-slim, Flask, bash

---

## Topología de red

```
Laptop A (Main) ─────────────────────────────────────────────
│  Ambassador  :5005   (servicio permanente)
│  Coordinator        (proceso one-shot, lanzado manualmente)
│
│  .env  →  WORKER_1_URL=http://<IP-B>:5001
│           WORKER_2_URL=http://<IP-C>:5001
│           WORKER_3_URL=http://<IP-D>:5001
└──────────────────────────────────────────────────────────────
        │ POST /count         │ POST /count       │ POST /count
        ▼                     ▼                   ▼
Laptop B :5001          Laptop C :5001      Laptop D :5001
  Worker 1                Worker 2            Worker 3
  wiki_es.txt             wiki_es.txt         wiki_es.txt
```

**Reglas de firewall necesarias:**
- Laptops B, C, D: abrir puerto **5001** (TCP entrada) desde cualquier IP de la red WiFi
- Laptop A: ningún puerto necesita abrirse (el Ambassador inicia las conexiones)

---

## Mapa de archivos

| Acción | Archivo | Responsabilidad |
|--------|---------|-----------------|
| Modificar | `docker-compose.yml` | Agregar `profiles: [coordinator]` para que coordinator no arranque automáticamente |
| Crear | `docker-compose.worker.yml` | Compose simplificado para las laptops worker (sin Ambassador ni Coordinator) |
| Crear | `scripts/verify-network.sh` | Verifica conectividad a todos los workers antes de lanzar el benchmark |
| Crear | `Makefile` | Comandos cortos para las operaciones más comunes del día de demo |
| Modificar | `README.md` | Guía de despliegue paso a paso para 4 laptops |

---

## Task 1: Aislar coordinator con Docker Compose profiles

**Problema actual:** `docker-compose up` lanza coordinator inmediatamente junto con el Ambassador. Pero el coordinator falla si los workers aún no están listos.

**Solución:** Mover coordinator al profile `coordinator`. Los servicios sin profile arrancan con `docker-compose up`; los que tienen profile solo arrancan con `--profile coordinator`.

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Leer el archivo actual**

```bash
cat docker-compose.yml
```

- [ ] **Step 2: Agregar `profiles` al servicio coordinator**

Ubicar el bloque `coordinator:` y agregar la clave `profiles` como primera línea del servicio:

```yaml
  coordinator:
    profiles: [coordinator]          # ← agregar esta línea
    build:
      context: .
      dockerfile: Dockerfile.coordinator
    restart: "no"
    environment:
      AMBASSADOR_URL: "http://ambassador:5005"
      FILE_PATH: "/app/wiki_es.txt"
      NUM_CHUNKS: "3"
    volumes:
      - ./wiki_es.txt:/app/wiki_es.txt:ro
    depends_on:
      - ambassador
```

- [ ] **Step 3: Verificar que `docker-compose up` no lanza el coordinator**

```bash
docker-compose config --services
```

Salida esperada (coordinator NO aparece sin el flag `--profile`):

```
ambassador
worker_local
```

- [ ] **Step 4: Verificar que el coordinator sí aparece con el profile**

```bash
docker-compose --profile coordinator config --services
```

Salida esperada:

```
ambassador
coordinator
worker_local
```

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(docker): aislar coordinator en profile para evitar arranque prematuro

Sin este cambio, docker-compose up lanzaba el coordinator inmediatamente
junto con el Ambassador, fallando porque los workers remotos aún no estaban
listos. Con profiles: [coordinator], el coordinator solo arranca cuando se
invoca explícitamente con --profile coordinator o via Makefile."
```

---

## Task 2: docker-compose para laptops worker

**Contexto:** Las laptops B, C, D solo necesitan correr el Worker. No tienen Ambassador ni Coordinator. Necesitan su propio archivo compose mínimo.

**Files:**
- Create: `docker-compose.worker.yml`

- [ ] **Step 1: Crear el archivo**

```yaml
# docker-compose.worker.yml — Para las laptops que solo corren el Worker.
#
# Uso:
#   WORKER_ID=worker_1 docker-compose -f docker-compose.worker.yml up --build
#
# Requerimientos:
#   - wiki_es.txt debe estar en el mismo directorio que este archivo
#   - El puerto 5001 debe estar abierto en el firewall de esta laptop

services:
  worker:
    build:
      context: .
      dockerfile: Dockerfile.worker
    ports:
      - "5001:5001"          # El Ambassador de la laptop principal conectará aquí
    environment:
      WORKER_ID: "${WORKER_ID:-worker_1}"   # Cambiar en cada laptop: worker_1, worker_2, worker_3
    volumes:
      - ./wiki_es.txt:/app/wiki_es.txt:ro  # El archivo NO entra a la imagen; se monta en runtime
    restart: on-failure
```

- [ ] **Step 2: Verificar sintaxis**

```bash
docker-compose -f docker-compose.worker.yml config
```

Salida esperada: el YAML expandido sin errores.

- [ ] **Step 3: Probar el health check del worker localmente**

```bash
WORKER_ID=worker_1 docker-compose -f docker-compose.worker.yml up --build -d
sleep 3
curl -s http://localhost:5001/health
```

Salida esperada:

```json
{"status": "ok", "worker_id": "worker_1"}
```

- [ ] **Step 4: Detener el worker de prueba**

```bash
docker-compose -f docker-compose.worker.yml down
```

- [ ] **Step 5: Commit**

```bash
git add docker-compose.worker.yml
git commit -m "feat(docker): agregar docker-compose.worker.yml para laptops worker

Compose mínimo para las laptops B, C, D que solo corren el Worker.
Configura WORKER_ID via variable de entorno (worker_1/2/3) y monta
wiki_es.txt desde el directorio local sin incluirlo en la imagen Docker."
```

---

## Task 3: Script de verificación de red

**Contexto:** Antes de lanzar el coordinator, necesitamos confirmar que el Ambassador puede llegar a los 3 workers. Este script lo verifica desde la laptop principal.

**Files:**
- Create: `scripts/verify-network.sh`

- [ ] **Step 1: Crear el directorio y el script**

```bash
mkdir -p scripts
```

Crear `scripts/verify-network.sh`:

```bash
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
```

- [ ] **Step 2: Dar permisos de ejecución**

```bash
chmod +x scripts/verify-network.sh
```

- [ ] **Step 3: Probar el script (con solo el Ambassador corriendo localmente)**

```bash
docker-compose up -d ambassador
sleep 2
bash scripts/verify-network.sh
```

Salida esperada (Ambassador OK, workers fallan porque aún no están en red):

```
=== Verificación de conectividad ===

✅  Ambassador  →  {"service":"ambassador","status":"ok"}
❌  Worker 1   →  SIN RESPUESTA en http://192.168.1.101:5001/health
❌  Worker 2   →  SIN RESPUESTA en http://192.168.1.102:5001/health
❌  Worker 3   →  SIN RESPUESTA en http://192.168.1.103:5001/health

=== Resultado: 1 OK  /  3 FALLO(S) ===
```

- [ ] **Step 4: Commit**

```bash
git add scripts/verify-network.sh
git commit -m "feat(scripts): agregar verify-network.sh para pre-flight check del demo

Verifica que Ambassador y los 3 workers respondan en /health antes de
lanzar el coordinator. Sale con código 1 si algún servicio falla,
con mensaje de diagnóstico sobre firewall."
```

---

## Task 4: Makefile con comandos del día de demo

**Contexto:** En el día de demo hay presión de tiempo. Un `Makefile` con targets claros evita que alguien escriba el comando largo de docker-compose equivocado.

**Files:**
- Create: `Makefile`

- [ ] **Step 1: Crear el Makefile**

```makefile
# Makefile — Comandos para el día de demo.
# Laptop principal (Ambassador + Coordinator).
#
# Uso rápido:
#   make start     → levanta el Ambassador (listo para recibir chunks)
#   make verify    → verifica que Ambassador + 3 workers respondan
#   make run       → lanza el coordinator (benchmark distribuido + secuencial)
#   make stop      → detiene todos los contenedores
#   make logs      → sigue los logs del Ambassador en tiempo real

.PHONY: start stop run verify logs build worker-local

## Levanta el Ambassador. No lanza el Coordinator (se lanza manualmente con 'make run').
start:
	docker-compose up -d --build ambassador

## Detiene y elimina todos los contenedores del proyecto.
stop:
	docker-compose --profile coordinator down

## Verifica conectividad con Ambassador y los 3 workers antes del benchmark.
verify:
	@bash scripts/verify-network.sh

## Lanza el Coordinator (benchmark distribuido + ground truth secuencial).
## Requiere: make start + make verify pasen sin errores.
run:
	docker-compose --profile coordinator run --rm coordinator

## Sigue los logs del Ambassador en tiempo real.
logs:
	docker-compose logs -f ambassador

## Levanta un worker local para pruebas en una sola laptop (sin red real).
worker-local:
	docker-compose up -d --build worker_local

## Construye todas las imágenes sin levantar servicios.
build:
	docker-compose --profile coordinator build
```

- [ ] **Step 2: Verificar que `make start` funciona**

```bash
make stop   # limpieza previa
make start
docker-compose ps
```

Salida esperada:

```
NAME                    STATUS
wordfrequency-ambassador-1   running
```

- [ ] **Step 3: Verificar que `make run` ejecuta y termina**

Con `WORKER_1_URL=http://localhost:5001` en `.env` y `make worker-local` corriendo:

```bash
make run
```

Salida esperada (últimas líneas):

```
[COORD HH:MM:SS] Tiempo secuencial: XX.XXs
[COORD HH:MM:SS] Speedup: X.XXx más rápido
```

- [ ] **Step 4: Commit**

```bash
git add Makefile
git commit -m "feat: agregar Makefile con comandos para el día de demo

Targets: start (Ambassador), verify (health checks), run (Coordinator),
stop, logs, build, worker-local. Reduce el riesgo de errores el día
de la presentación con comandos cortos y documentados."
```

---

## Task 5: Actualizar README con guía de despliegue

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Reescribir el README con la guía completa**

El README debe cubrir, en este orden:

1. **Descripción del proyecto** (2-3 líneas)
2. **Arquitectura** (diagrama ASCII de la topología)
3. **Requisitos** (Docker, Docker Compose v2, wiki_es.txt en cada laptop)
4. **Configuración de red** (cómo obtener la IP de cada laptop)
5. **Laptops Worker (B, C, D)** — paso a paso:
   - Clonar/copiar el repositorio
   - Colocar wiki_es.txt en el directorio raíz
   - Abrir el puerto 5001 en el firewall
   - Ejecutar `WORKER_ID=worker_N docker-compose -f docker-compose.worker.yml up --build`
6. **Laptop Principal (A)** — paso a paso:
   - Copiar `.env.example → .env` y poner las IPs reales
   - Ejecutar `make start`
   - Ejecutar `make verify` — esperar que los 3 workers aparezcan en verde
   - Ejecutar `make run` — lanza el benchmark
7. **Solución de problemas de firewall**
   - Windows: `netsh advfirewall firewall add rule name="Worker 5001" protocol=TCP dir=in localport=5001 action=allow`
   - macOS: Sistema → Privacidad → Firewall → Opciones → Agregar Docker
8. **Comandos de referencia** (tabla con todos los `make` targets)

- [ ] **Step 2: Verificar que el README no tiene referencias a puertos incorrectos**

```bash
grep -n "5000\|:4999\|:5000" README.md
```

Salida esperada: ninguna línea (el Ambassador usa 5005, el Coordinator no expone puerto).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): reescribir guía de despliegue para 4 laptops en red

Cubre configuración de Ambassador, 3 workers remotos, firewall en
Windows/macOS, comandos Makefile, diagrama de topología y solución
de problemas frecuentes para el día de demo."
```

---

## Task 6: Prueba de integración completa (una sola laptop)

**Objetivo:** Verificar el flujo completo localmente antes de la demo real con 4 laptops.

**Files:**
- Ninguno (usa archivos existentes: `test_components.py`)

- [ ] **Step 1: Levantar toda la pila localmente**

```bash
make stop          # limpieza
cp .env.example .env
# Editar .env para modo local: apuntar los 3 workers a worker_local
# WORKER_1_URL=http://worker_local:5001
# WORKER_2_URL=http://worker_local:5001
# WORKER_3_URL=http://worker_local:5001
make start
make worker-local
```

- [ ] **Step 2: Verificar que Ambassador y worker_local responden**

```bash
make verify
```

Salida esperada (todos en verde):

```
✅  Ambassador  →  {"service":"ambassador","status":"ok"}
✅  Worker 1   →  {"status":"ok","worker_id":"worker_local"}
✅  Worker 2   →  {"status":"ok","worker_id":"worker_local"}
✅  Worker 3   →  {"status":"ok","worker_id":"worker_local"}
=== Resultado: 4 OK  /  0 FALLO(S) ===
```

- [ ] **Step 3: Ejecutar el test de componentes existente**

```bash
python test_components.py
```

Salida esperada: chunks procesados con status "ok" y conteo de palabras.

- [ ] **Step 4: Ejecutar el benchmark completo**

```bash
make run
```

Verificar en la salida:
- `Chunks exitosos : 3 / 3`
- Speedup calculado (puede ser < 1x en una sola laptop por overhead de Docker)
- Top 20 palabras en español

- [ ] **Step 5: Limpiar**

```bash
make stop
```

---

## Secuencia del día de demo

```
LAPTOPS B, C, D (workers) — hacer esto primero:
  1. git pull  (o copiar el repo)
  2. Asegurarse de que wiki_es.txt está en el directorio
  3. Abrir puerto 5001 en el firewall
  4. WORKER_ID=worker_N docker-compose -f docker-compose.worker.yml up --build

LAPTOP A (main) — hacer esto después:
  1. Editar .env con las IPs reales de B, C, D
  2. make start
  3. make verify  →  esperar 4 ✅
  4. make run     →  el profesor ve los logs del benchmark
```
