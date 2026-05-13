# Distributed Word-Frequency Counter

Cuenta la frecuencia de palabras en la Wikipedia en español (~5 GB) repartiendo el trabajo entre varias computadoras. Cada computadora lee su propia copia del archivo — **el archivo nunca viaja por red**.

---

## Índice

1. [Qué hace el sistema](#qué-hace-el-sistema)
2. [Requisitos previos](#requisitos-previos)
3. [Prueba rápida — una sola computadora](#prueba-rápida--una-sola-computadora)
4. [Demo real — 4 computadoras en red](#demo-real--4-computadoras-en-red)
5. [Comandos de referencia](#comandos-de-referencia)
6. [Verificar que todo funciona](#verificar-que-todo-funciona)
7. [Solución de problemas](#solución-de-problemas)
8. [Cómo funciona por dentro](#cómo-funciona-por-dentro)

---

## Qué hace el sistema

1. El **Coordinator** divide `wiki_es.txt` en 3 rangos de bytes iguales y los manda en paralelo.
2. El **Ambassador** recibe cada rango, elige un Worker y lo reenvía. Si un Worker falla, lo reintenta con otro.
3. Cada **Worker** lee su rango del disco local, cuenta palabras españolas con regex y devuelve un diccionario.
4. El Coordinator combina los 3 diccionarios y calcula el Speedup frente al conteo secuencial.

```
Tu terminal
    │
    │  POST /dispatch {chunk_id, inicio, fin}
    ▼
Ambassador :5005  ──────────────────────────────────────────────
    │                    │                         │
    │ POST /count        │ POST /count             │ POST /count
    │ {start, end}       │ {start, end}            │ {start, end}
    ▼                    ▼                         ▼
Worker 1 :5001      Worker 2 :5001           Worker 3 :5001
lee bytes 0..1.7GB  lee bytes 1.7..3.5GB     lee bytes 3.5..5.2GB
   wiki_es.txt         wiki_es.txt              wiki_es.txt
  (copia local)       (copia local)            (copia local)
```

---

## Requisitos previos

### En TODAS las computadoras

**1. Docker Desktop** — el motor que corre los contenedores.

- Descargarlo de: https://www.docker.com/products/docker-desktop/
- Instalarlo y abrirlo. Esperar a que el ícono de Docker en la barra de tareas deje de girar.
- Verificar que funciona:
  ```bash
  docker --version
  # Debe mostrar algo como: Docker version 24.x.x
  ```

**2. El archivo `wiki_es.txt`** — el corpus de ~5 GB que se va a contar.

- Copiarlo a la **carpeta raíz del repositorio** (la misma carpeta donde está `docker-compose.yml`).
- Verificar que está ahí:
  ```bash
  ls -lh wiki_es.txt
  # Debe mostrar algo como: -rw-r--r-- 1 user staff 4.9G wiki_es.txt
  ```

**3. El repositorio clonado**

```bash
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter
```

### Solo en macOS — herramienta `make`

```bash
xcode-select --install
# Si ya está instalada, mostrará un error — está bien, continúa.
```

### Solo en Windows — usar Git Bash

Todos los comandos de este README deben ejecutarse en **Git Bash**, no en CMD ni PowerShell.

---

## Prueba rápida — una sola computadora

Esta opción usa un solo Worker local que procesa los 3 chunks secuencialmente. Sirve para verificar que todo el código funciona antes del demo real.

> **Nota para agentes de IA:** ejecutar cada paso en orden. Verificar la salida esperada antes de continuar al siguiente.

### Paso 1 — Configurar las variables de entorno

```bash
cp .env.example .env
```

Verificar que `.env` tiene exactamente este contenido:

```
WORKER_1_URL=http://worker_local:5001
WORKER_2_URL=http://worker_local:5001
WORKER_3_URL=http://worker_local:5001
```

Si no lo tiene, editarlo:
```bash
cat > .env << 'EOF'
WORKER_1_URL=http://worker_local:5001
WORKER_2_URL=http://worker_local:5001
WORKER_3_URL=http://worker_local:5001
EOF
```

### Paso 2 — Construir y levantar el Ambassador

```bash
make start
```

Salida esperada (las últimas líneas):
```
Container word-frecuency-counter-ambassador-1 Started
```

Verificar que responde:
```bash
curl http://localhost:5005/health
```
Respuesta esperada:
```json
{"service": "ambassador", "status": "ok"}
```

Si `curl` no está instalado, usar:
```bash
python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:5005/health').read().decode())"
```

### Paso 3 — Construir y levantar el Worker local

```bash
make worker-local
```

Salida esperada (las últimas líneas):
```
Container word-frecuency-counter-worker_local-1 Started
```

Verificar que responde:
```bash
curl http://localhost:5001/health
```
Respuesta esperada:
```json
{"status": "ok", "worker_id": "worker_local"}
```

### Paso 4 — Verificar el estado de los Circuit Breakers

Los 3 Circuit Breakers deben estar en CLOSED (verde):

```bash
curl -s http://localhost:5005/workers/status
```

Respuesta esperada:
```json
{
  "activos": ["worker_1", "worker_2", "worker_3"],
  "n": 3,
  "detalle": {
    "worker_1": {"state": "CLOSED", "fail_count": 0},
    "worker_2": {"state": "CLOSED", "fail_count": 0},
    "worker_3": {"state": "CLOSED", "fail_count": 0}
  }
}
```

Si alguno está en OPEN, esperar 15 segundos y repetir. El health poller los recupera automáticamente.

### Paso 5 — Probar un chunk pequeño (opcional pero recomendado)

Antes del benchmark completo, probar con 1 MB para confirmar el flujo:

```bash
curl -s -X POST http://localhost:5005/dispatch \
  -H "Content-Type: application/json" \
  -d '{"chunk_id": 0, "inicio": 0, "fin": 1000000}'
```

Respuesta esperada (fragmento):
```json
{
  "chunk_id": 0,
  "status": "ok",
  "worker_id": "worker_1",
  "result": {"de": 1234, "la": 987, "el": 856, ...}
}
```

Si la respuesta es `"status": "error"`, ver la sección [Solución de problemas](#solución-de-problemas).

### Paso 6 — Benchmark completo

```bash
make run
```

Esto lanza el Coordinator dentro de Docker. Divide el archivo en 3 chunks (~1.7 GB cada uno), los procesa y luego hace el conteo secuencial para calcular el Speedup.

Salida esperada al final:
```
[COORD HH:MM:SS] Chunks exitosos : 3 / 3
[COORD HH:MM:SS] Tiempo distribuido : XX.XXs
[COORD HH:MM:SS] Palabras únicas    : X,XXX,XXX
[COORD HH:MM:SS] Top 20 palabras:
[COORD HH:MM:SS]   de                   XX,XXX,XXX
[COORD HH:MM:SS]   la                   XX,XXX,XXX
...
[COORD HH:MM:SS] Tiempo secuencial: XX.XXs
[COORD HH:MM:SS] Speedup: X.XXx más rápido
```

> **Advertencia sobre tiempos en una sola laptop:** Docker Desktop en macOS/Windows virtualiza el acceso al disco, haciendo las lecturas de 5 GB mucho más lentas que en hardware real. El benchmark puede tardar varios minutos. El Speedup probablemente sea < 1x — eso es normal porque hay un solo disco físico. En 4 laptops reales, cada una lee su rango de su propio disco en paralelo y el Speedup sube.

### Paso 7 — Apagar todo

```bash
make stop
```

---

## Demo real — 4 computadoras en red

### Antes de empezar

Todas las laptops deben estar conectadas a la **misma red WiFi**. Conseguir la IP de cada una:

```bash
# macOS:
ifconfig en0 | grep "inet " | awk '{print $2}'

# Windows (Git Bash):
ipconfig | grep "IPv4" | head -1
```

Anotar las IPs: por ejemplo, `192.168.1.101`, `192.168.1.102`, `192.168.1.103`.

---

### Laptops Worker (B, C, D) — hacer PRIMERO

Repetir estos pasos en cada una de las 3 laptops worker. Asignar un `WORKER_ID` diferente a cada una (`worker_1`, `worker_2`, `worker_3`).

**Paso 1 — Clonar el repo y copiar el archivo**

```bash
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter
# Copiar wiki_es.txt a esta carpeta
ls -lh wiki_es.txt  # verificar que pesa ~5 GB
```

**Paso 2 — Abrir el puerto 5001 en el firewall**

El Ambassador necesita conectarse a este puerto desde la laptop principal.

macOS:
- Sistema → Privacidad y Seguridad → Firewall → Opciones → agregar Docker → Permitir conexiones entrantes

Windows (PowerShell como administrador):
```powershell
netsh advfirewall firewall add rule name="Worker 5001" protocol=TCP dir=in localport=5001 action=allow
```

**Paso 3 — Levantar el Worker**

Cambiar `N` por el número de esta laptop (`1`, `2` o `3`):

```bash
WORKER_ID=worker_N docker-compose -f docker-compose.worker.yml up --build
```

La terminal muestra los logs en tiempo real. Dejar esta ventana abierta.

**Paso 4 — Verificar que responde**

Abrir otra terminal y ejecutar:

```bash
curl http://localhost:5001/health
# Respuesta esperada: {"status":"ok","worker_id":"worker_N"}
```

---

### Laptop Principal (A) — hacer DESPUÉS

**Paso 1 — Clonar el repo**

```bash
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter
```

> La laptop principal también necesita `wiki_es.txt` para el conteo secuencial (ground truth).

**Paso 2 — Configurar las IPs de los workers**

```bash
cp .env.example .env
```

Editar `.env` reemplazando las IPs con las reales:

```
WORKER_1_URL=http://192.168.1.101:5001
WORKER_2_URL=http://192.168.1.102:5001
WORKER_3_URL=http://192.168.1.103:5001
```

**Paso 3 — Levantar el Ambassador**

```bash
make start
```

**Paso 4 — Verificar conectividad con todos los workers**

```bash
make verify
```

Salida esperada (4 checks en verde):
```
=== Verificación de conectividad ===

✅  Ambassador  →  {"service":"ambassador","status":"ok"}
✅  Worker 1   →  {"status":"ok","worker_id":"worker_1"}
✅  Worker 2   →  {"status":"ok","worker_id":"worker_2"}
✅  Worker 3   →  {"status":"ok","worker_id":"worker_3"}

=== Resultado: 4 OK  /  0 FALLO(S) ===

🟢  Todos los servicios responden. Puedes lanzar el coordinator.
```

Si algún Worker muestra ❌, revisar que su puerto 5001 esté abierto y el contenedor corriendo.

**Paso 5 — Lanzar el benchmark**

```bash
make run
```

Los logs del Coordinator aparecen en la terminal. El benchmark tarda según el hardware de las laptops. Al terminar muestra el Speedup.

---

## Comandos de referencia

### Laptop principal

| Comando | Descripción |
|---------|-------------|
| `make start` | Levanta el Ambassador (reconstruye si hay cambios en el código) |
| `make stop` | Detiene y elimina todos los contenedores |
| `make verify` | Verifica Ambassador + 3 workers con health checks |
| `make run` | Lanza el Coordinator (benchmark completo) |
| `make worker-local` | Levanta un Worker local (para prueba en una sola laptop) |
| `make logs` | Sigue los logs del Ambassador en tiempo real |
| `make build` | Construye todas las imágenes sin levantar nada |

### Laptops worker

| Comando | Descripción |
|---------|-------------|
| `WORKER_ID=worker_N docker-compose -f docker-compose.worker.yml up --build` | Levanta el Worker |
| `docker-compose -f docker-compose.worker.yml down` | Detiene el Worker |
| `curl http://localhost:5001/health` | Verifica que el Worker responde |

---

## Verificar que todo funciona

### Health checks manuales

```bash
# Ambassador
curl http://localhost:5005/health
# → {"service": "ambassador", "status": "ok"}

# Worker (desde la laptop worker o desde la principal si es worker_local)
curl http://localhost:5001/health
# → {"status": "ok", "worker_id": "worker_N"}

# Estado de todos los Circuit Breakers (desde la laptop principal)
curl http://localhost:5005/workers/status
# → {"activos": ["worker_1","worker_2","worker_3"], "n": 3, ...}
```

### Prueba de un chunk pequeño

```bash
curl -s -X POST http://localhost:5005/dispatch \
  -H "Content-Type: application/json" \
  -d '{"chunk_id": 0, "inicio": 0, "fin": 1000000}'
# → {"chunk_id": 0, "status": "ok", "result": {"de": ..., "la": ...}}
```

---

## Solución de problemas

### Error: `make: command not found`

Instalar `make`:
- macOS: `xcode-select --install`
- Windows: usar Git Bash (ya incluye `make`) o instalar desde https://gnuwin32.sourceforge.net/packages/make.htm

### El Ambassador no arranca: `port is already allocated`

Otro proceso usa el puerto 5005 (en macOS puede ser AirPlay Receiver):

```bash
# Ver qué usa el puerto:
lsof -i :5005

# Desactivar AirPlay en macOS:
# Sistema → General → AirDrop y Handoff → AirPlay Receiver → desactivar
```

### `make run` da 400 BAD REQUEST en los workers

El contenedor del Worker está corriendo código viejo. Reconstruirlo:

```bash
make stop
make start
make worker-local   # si es prueba local
make run
```

### `make run` da 503 SERVICE UNAVAILABLE

Los Circuit Breakers están en OPEN. Esperar 15 segundos y el health poller los cierra automáticamente:

```bash
# Ver el estado:
curl -s http://localhost:5005/workers/status

# Ver los logs del Ambassador para entender qué pasó:
make logs
```

Si persiste, reiniciar todo:
```bash
make stop && make start && make worker-local && make run
```

### `make run` da 502 BAD GATEWAY

El Ambassador agotó los 3 reintentos en todos los workers. Causas frecuentes:

1. **El Worker crasheó** — revisar si el contenedor sigue corriendo:
   ```bash
   docker ps
   # Si worker_local no aparece o STATUS dice "Restarting", el Worker crasheó
   ```
   Solución: `make worker-local` para reconstruirlo.

2. **Timeout por archivo grande en Docker local** — Docker Desktop virtualiza el disco y las lecturas de 1.7 GB son lentas. Esperar a que el sistema tenga menos carga o probar con un chunk pequeño primero.

### Error: `wiki_es.txt: No such file or directory`

El archivo no está en el lugar correcto. Verificar:

```bash
ls -lh wiki_es.txt
# Debe mostrar ~5 GB en la carpeta raíz del repositorio
```

Si no está, copiarlo a `Word-Frecuency-Counter/wiki_es.txt`.

### Workers remotos no responden en `make verify`

El firewall bloquea el puerto 5001:

```bash
# Windows (PowerShell como administrador):
netsh advfirewall firewall add rule name="Worker 5001" protocol=TCP dir=in localport=5001 action=allow

# macOS:
# Sistema → Privacidad y Seguridad → Firewall → Opciones → Docker → Permitir conexiones
```

También verificar que el Worker está corriendo en la laptop remota:
```bash
# Desde la laptop remota:
curl http://localhost:5001/health
```

### Los CBs no se recuperan solos

Si `curl http://localhost:5005/workers/status` muestra workers en OPEN después de 30 segundos, reiniciar el Ambassador:

```bash
make stop && make start
```

Esto resetea todos los Circuit Breakers a CLOSED.

---

## Cómo funciona por dentro

### Circuit Breaker — protección ante fallos

Cada Worker tiene su propio Circuit Breaker (CB) con 3 estados:

```
CLOSED 🟢 → operación normal
  ↓ 3 fallos consecutivos
OPEN 🔴   → fast-fail: el Ambassador salta al siguiente worker sin intentarlo
  ↓ 10 segundos
HALF_OPEN 🟡 → se permite UNA petición de prueba
  ↓ éxito → CLOSED 🟢
  ↓ fallo → OPEN 🔴
```

El Ambassador también tiene un **health poller** que revisa `/health` de cada Worker cada 10 segundos. Si detecta un Worker caído (connection refused), lo marca en el CB sin esperar al siguiente chunk.

### Traducción de campos

El Coordinator envía los rangos con nombres en español (`inicio`, `fin`). El Worker espera inglés (`start`, `end`). El Ambassador hace la traducción internamente — ni el Coordinator ni el Worker necesitan cambiarse.

### Lectura por bloques

Cada Worker lee su chunk de 1.7 GB en bloques de 64 MB para evitar usar toda la RAM. Procesa cada bloque con regex y acumula el contador de palabras.

---

## Variables de entorno

### `.env` (laptop principal — leído por el Ambassador)

```bash
# URLs de los 3 workers (editar con las IPs reales)
WORKER_1_URL=http://192.168.1.101:5001
WORKER_2_URL=http://192.168.1.102:5001
WORKER_3_URL=http://192.168.1.103:5001

# Timeout por petición al worker (default: 120s)
# En hardware real con disco NVMe, 20s es suficiente.
# En Docker Desktop macOS con archivo de 5GB, dejar en 120s.
# REQUEST_TIMEOUT=120
```

### Variables del Coordinator (`docker-compose.yml`)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `AMBASSADOR_URL` | `http://ambassador:5005` | URL del Ambassador (nombre Docker interno) |
| `FILE_PATH` | `/app/wiki_es.txt` | Ruta al archivo dentro del contenedor |
| `NUM_CHUNKS` | `3` | En cuántas partes dividir el archivo |

### Variables del Worker (`docker-compose.worker.yml`)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `WORKER_ID` | `worker_1` | Nombre único. Usar `worker_1`, `worker_2`, `worker_3` |
| `WIKI_PATH` | `/app/wiki_es.txt` | Ruta al archivo dentro del contenedor |
| `DELAY` | `0` | Segundos de latencia artificial (para probar CB) |
| `FAIL_MODE` | `false` | Si `true`, devuelve HTTP 500 siempre (para probar CB) |

---

## Limitación conocida: prueba local en Docker Desktop macOS/Windows

En una sola laptop, los 3 chunks (1.7 GB cada uno) se procesan secuencialmente por el mismo Worker a través del filesystem virtualizado de Docker. Esto es significativamente más lento que en hardware real:

- **Docker Desktop macOS/Windows:** 3-5 minutos por benchmark
- **4 laptops reales (Linux/macOS con NVMe):** 30-90 segundos

El Speedup en modo local será < 1x — eso es correcto y esperado. En el demo real con 4 laptops, cada una lee desde su propio disco físico en paralelo y el Speedup sube.

---

*Proyecto universitario — Sistemas Distribuidos.*
