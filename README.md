# Distributed Word-Frequency Counter

Sistema distribuido para contar la frecuencia de palabras en archivos de texto masivos (Wikipedia en espanol, 5.2 GB) utilizando una arquitectura de microservicios con registro dinamico de workers, balanceo de carga, Circuit Breaker, tolerancia a fallos y reintentos automaticos.

## Arquitectura del Sistema

El sistema distribuye el procesamiento en multiples computadoras (nodos). Cada nodo tiene su propia copia local del archivo — solo se transmiten metadatos (rangos de bytes) y diccionarios de frecuencia (JSON) por red.

| Componente | Puerto | Responsabilidad |
|------------|--------|-----------------|
| **Coordinator** | 4999 | Divide el archivo en chunks (rangos de bytes), los despacha en paralelo al Ambassador, combina resultados. Maneja estados: `idle`, `running`, `done`, `partial`, `error`. |
| **Ambassador** | 5005 | Proxy inteligente: registra workers dinamicamente, balancea carga con Round-Robin, gestiona Circuit Breakers por worker, aplica reintentos con rotacion, anti-doble-asignacion. |
| **Workers** | 5001 | Servidores Flask que reciben un rango de bytes, leen su copia local del archivo, extraen palabras con regex y retornan el conteo. Se auto-registran con el Ambassador al arrancar. |

### Flujo de datos

```
Worker ──POST /register──> Ambassador          (auto-registro al arrancar)

Coordinator ──POST /dispatch──> Ambassador ──POST /count──> Worker
                                    |                         |
                                    |   Round-Robin +         |  seek(start)
                                    |   Circuit Breaker +     |  read en bloques de 64MB
                                    |   busy check            |  regex + Counter
                                    |                         |
                                    <────── JSON {word:count} <
```

> **Dato clave:** El archivo `wiki_es.txt` reside localmente en cada nodo. El sistema solo intercambia offsets de inicio/fin y diccionarios JSON por red.

---

## Endpoints

### Coordinator (:4999)

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| `POST` | `/start` | Arranca el procesamiento. Body opcional: `{"retries": N, "ground_truth": false}`. Hace health check de workers antes de despachar. |
| `GET` | `/status` | Estado actual: workers, chunks en proceso, chunks lentos (>60s). |
| `GET` | `/result` | Resultado del ultimo procesamiento e historial (ultimas 5 ejecuciones). |
| `POST` | `/reset` | Vuelve a `idle` desde `done`/`partial`/`error`. Limpia resultado y counter acumulado. |
| `POST` | `/retry-failed` | Re-despacha solo los chunks fallidos y fusiona con el counter existente. Solo desde estado `partial`. |

### Ambassador (:5005)

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| `POST` | `/register` | Registra un worker. Body: `{"worker_id": "...", "url": "..."}`. Resetea CB si el worker se re-registra con circuito abierto. |
| `DELETE` | `/workers/<worker_id>` | Da de baja un worker manualmente. |
| `POST` | `/dispatch` | Recibe un chunk del Coordinator y lo despacha a un worker disponible con reintentos. |
| `GET` | `/workers/status` | Estado del pool: workers registrados, activos, ocupados, estado de cada CB. |
| `GET` | `/workers/health` | Health check paralelo de todos los workers (timeout 3s cada uno). |
| `POST` | `/workers/reset-cbs` | Resetea todos los Circuit Breakers a CLOSED. |
| `GET` | `/health` | Health check del Ambassador. |

### Worker (:5001)

| Metodo | Ruta | Descripcion |
|--------|------|-------------|
| `GET` | `/health` | Estado: uptime, accesibilidad del archivo, URL registrada. |
| `POST` | `/count` | Procesa rango `[start, end)` y retorna `{word: count, ...}`. |

---

## Tecnologias

* **Lenguaje:** Python 3.11
* **Web Framework:** Flask (servidores REST multi-hilo)
* **Comunicacion HTTP:** Libreria `requests`
* **Infraestructura:** Docker y Docker Compose
* **Tolerancia a fallos:** Circuit Breaker (3 estados), reintentos con rotacion, auto-reintento

---

## Requisitos Previos

1. [Docker Desktop](https://www.docker.com/products/docker-desktop/) instalado en cada computadora.
2. El archivo `wiki_es.txt` (5.2 GB) en **cada** computadora.
3. Para modo red: todas las computadoras conectadas a la **misma red WiFi/LAN**.

---

## Ejecucion Local (una sola maquina)

Para pruebas rapidas sin necesidad de red:

```bash
# 1. Clonar el repositorio
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter
git checkout coordinator

# 2. Colocar wiki_es.txt en la raiz del proyecto

# 3. Levantar todo (Ambassador + Worker local + Coordinator)
docker compose up --build

# 4. Esperar a ver en logs que el worker se registro y el ambassador esta healthy

# 5. Lanzar el procesamiento
curl -X POST http://localhost:4999/start

# 6. Monitorear
curl http://localhost:4999/status
curl http://localhost:4999/result

# 7. Detener
docker compose down
```

> El `docker-compose.yml` incluye un `worker_local` que se auto-registra con el Ambassador. No se necesita archivo `.env`.

---

## Despliegue en Red (multiples computadoras)

### Topologia

```
  TU PC (Coordinator + Ambassador + Worker local)
  +---------------------------------------------+
  |  docker compose up --build                   |
  |  - ambassador  :5005                         |
  |  - coordinator :4999                         |
  |  - worker_local :5001                        |
  +----------------------+-----------------------+
                         |  red WiFi/LAN
                 +-------+-------+
                 |               |
                 v               v
              Laptop A        Laptop B
              Worker A        Worker B
              :5002           :5002
```

Los workers remotos se **auto-registran** con el Ambassador al arrancar. No se necesita configurar IPs de workers en tu PC.

---

### Paso 1: Tu PC — levantar los servicios principales

```bash
cd Word-Frecuency-Counter
docker compose up --build
```

Obtener tu IP local:

**Windows:**
```
ipconfig
# Buscar "Adaptador Wi-Fi" → "Direccion IPv4" (ej. 192.168.1.10)
```

**macOS:**
```
ifconfig en0
# Buscar "inet" (ej. 192.168.1.10)
```

---

### Paso 2: Cada laptop remota — levantar el worker

En cada laptop worker:

```bash
# 1. Clonar el repositorio
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter
git checkout coordinator

# 2. Asegurar que wiki_es.txt esta disponible (ej. en C:\wiki_es.txt)

# 3. Construir la imagen
docker build -f Dockerfile.worker -t wordcounter-worker .

# 4. Obtener la IP local de esta laptop (ipconfig / ifconfig)

# 5. Ejecutar el worker
#    Reemplazar:
#      <IP_TU_PC>       → IP de tu PC (donde corre el Ambassador)
#      <IP_ESTA_LAPTOP> → IP de esta laptop worker
#      <RUTA_ARCHIVO>   → Ruta al wiki_es.txt en esta laptop
```

**Windows (PowerShell):**
```powershell
docker run --rm `
  -p 5002:5001 `
  -e WORKER_ID=worker_laptopX `
  -e AMBASSADOR_URL=http://<IP_TU_PC>:5005 `
  -e WORKER_URL=http://<IP_ESTA_LAPTOP>:5002 `
  -v <RUTA_ARCHIVO>:/app/data/input.txt:ro `
  wordcounter-worker
```

**Linux/macOS (bash):**
```bash
docker run --rm \
  -p 5002:5001 \
  -e WORKER_ID=worker_laptopX \
  -e AMBASSADOR_URL=http://<IP_TU_PC>:5005 \
  -e WORKER_URL=http://<IP_ESTA_LAPTOP>:5002 \
  -v <RUTA_ARCHIVO>:/app/data/input.txt:ro \
  wordcounter-worker
```

> Usar un `WORKER_ID` unico por laptop (ej. `worker_laptop2`, `worker_laptop3`).
> El puerto externo (`-p 5002:5001`) puede variar si hay conflictos.

Si Docker no puede descargar la imagen base (`python:3.11-slim`) por problemas de red, exportar desde tu PC con `docker save wordcounter-worker -o worker_image.tar`, copiar el archivo a la laptop, y cargar con `docker load -i worker_image.tar`.

---

### Paso 3: Verificar conexion

Desde tu PC:

```bash
# Ver workers registrados
curl http://localhost:5005/workers/status

# Health check de todos los workers
curl http://localhost:5005/workers/health
```

Ambos workers deben aparecer con `"reachable": true` y `"file_accessible": true`.

---

### Paso 4: Lanzar el procesamiento

```bash
# Lanzar con ground truth (compara contra conteo secuencial)
curl -X POST http://localhost:4999/start

# O sin ground truth (mas rapido, omite conteo secuencial)
curl -X POST http://localhost:4999/start -H "Content-Type: application/json" -d "{\"ground_truth\": false}"
```

---

### Paso 5: Monitorear y obtener resultados

```bash
# Estado en tiempo real (chunks en proceso, chunks lentos)
curl http://localhost:4999/status

# Resultado final (top 20 palabras, speedup, historial)
curl http://localhost:4999/result
```

---

## Manejo de Fallos

### Si el resultado es `partial` (algunos chunks fallaron)

```bash
# Ver que chunks fallaron
curl http://localhost:4999/result
# → chunks_fallidos: [{chunk_id, inicio, fin, reason}]

# Reintentar solo los chunks fallidos (sin repetir los exitosos)
curl -X POST http://localhost:4999/retry-failed
```

### Para volver a ejecutar todo desde cero

```bash
# 1. Resetear el coordinator
curl -X POST http://localhost:4999/reset

# 2. Resetear los Circuit Breakers (limpia bloqueos de corridas anteriores)
curl -X POST http://localhost:5005/workers/reset-cbs

# 3. Lanzar de nuevo
curl -X POST http://localhost:4999/start
```

### Si un worker se desconecto y volvio

El worker se re-registra automaticamente al arrancar. El Ambassador resetea su Circuit Breaker a CLOSED al detectar el re-registro.

---

## Solucion de Problemas

### El worker remoto no aparece en /workers/status

**Causa mas probable: Firewall.** Windows bloquea conexiones entrantes por defecto.

**Windows** — abrir el puerto (ejecutar como Administrador):
```
netsh advfirewall firewall add rule name="Worker WordCounter" dir=in action=allow protocol=TCP localport=5002
```

Para eliminar la regla despues:
```
netsh advfirewall firewall delete rule name="Worker WordCounter"
```

Tambien abrir el puerto 5005 en tu PC si el worker no puede registrarse:
```
netsh advfirewall firewall add rule name="Ambassador WordCounter" dir=in action=allow protocol=TCP localport=5005
```

### /workers/health muestra `reachable: false`

- Verificar que la IP usada en `WORKER_URL` es la IP **local de red** (192.168.x.x o 10.x.x.x), no una IP publica ni localhost.
- Verificar conectividad: `ping <IP_WORKER>` desde tu PC.
- Verificar que el contenedor esta corriendo: `docker ps` en la laptop worker.

### /workers/health muestra `file_accessible: false`

El archivo no esta montado correctamente. Verificar:
- Que `wiki_es.txt` existe en la ruta especificada en `-v`.
- Que la ruta en `-v` es absoluta (ej. `C:\wiki_es.txt:/app/data/input.txt:ro`).

### Docker no puede descargar `python:3.11-slim`

Red universitaria o VPN bloqueando Docker Hub. Solucion:
```bash
# En una PC con internet:
docker save wordcounter-worker -o worker_image.tar

# Copiar worker_image.tar a la laptop (USB, red local, etc.)

# En la laptop sin internet:
docker load -i worker_image.tar
```

### Las IPs cambiaron

Las IPs WiFi son dinamicas. Obtener las IPs justo antes de iniciar. Si una IP cambio, el worker se puede volver a registrar con la nueva IP.

---

## Parametros de Configuracion (config.py)

| Parametro | Valor | Descripcion |
|-----------|-------|-------------|
| `AMBASSADOR_PORT` | 5005 | Puerto del Ambassador |
| `FAIL_MAX` | 2 | Fallos consecutivos para abrir el Circuit Breaker |
| `RESET_TIMEOUT` | 10s | Segundos en OPEN antes de pasar a HALF_OPEN |
| `MAX_RETRIES` | 2 | Intentos por chunk en el Ambassador (2 = 1 reintento real) |
| `REQUEST_TIMEOUT` | 600s | Timeout HTTP para cada chunk (10 min) |
| `BUFFER_SIZE` | 64 MB | Tamano de bloque de lectura en el worker |
| `BOUNDARY_BUF` | 512 B | Bytes extra para capturar palabras en el limite de chunk |

---

## Pruebas

### Tests unitarios del Circuit Breaker
```bash
python -m pytest test_circuit_breaker.py -v
```
23 tests que cubren los 3 estados, transiciones, thread-safety y la interfaz `call()`.

### Ground Truth (baseline secuencial)
```bash
python ground_truth.py
```
Conteo secuencial en un solo hilo para comparar con el sistema distribuido.

---

## Estructura del Proyecto

```
WordCounter/
  coordinator.py           # Orquestador: divide, despacha, combina, estados
  ambassador.py            # Proxy: registro dinamico, Round-Robin, Circuit Breaker, reintentos
  worker.py                # Servidor Worker: conteo de palabras por rango de bytes
  circuit_breaker.py       # Circuit Breaker thread-safe (CLOSED/OPEN/HALF_OPEN)
  config.py                # Configuracion centralizada (puertos, timeouts, parametros)
  docker-compose.yml       # Orquestacion: Ambassador + Worker local + Coordinator
  Dockerfile.ambassador    # Imagen Docker del Ambassador
  Dockerfile.coordinator   # Imagen Docker del Coordinator
  Dockerfile.worker        # Imagen Docker del Worker
  requirements.txt         # Dependencias Python (flask, requests)
  ground_truth.py          # Conteo secuencial para benchmark
  test_circuit_breaker.py  # Tests unitarios del Circuit Breaker (pytest)
  test_components.py       # Tests de integracion mock
  docs/
    diagramas.md           # Diagramas Mermaid (arquitectura, secuencia, CB, componentes)
```

---

## Decisiones de Diseno

| Decision | Razon |
|----------|-------|
| **Registro dinamico de workers** | Los workers se auto-registran al arrancar. No se necesita configurar IPs manualmente en el Ambassador. |
| **Puerto 5005 para el Ambassador** | Evita conflicto con AirPlay Receiver de macOS (puerto 5000). |
| **Timeout de 600 segundos** | Permite que workers con discos lentos o virtualizacion Docker completen chunks de ~1.7 GB. |
| **Archivo local en cada nodo** | Evita transferir 5.2 GB por red; solo se intercambian metadatos JSON (KB). |
| **Bind mount (no COPY)** | El archivo no entra en la imagen Docker; la imagen pesa ~150 MB en vez de 5.3 GB. |
| **Round-Robin con Circuit Breaker** | Distribuye carga equitativamente y excluye workers caidos sin esperar timeout. |
| **Anti-doble-asignacion (busy set)** | Evita enviar un segundo chunk a un worker que aun no termino el primero. |
| **Reintentos con rotacion (MAX_RETRIES=2)** | Si un worker falla, el Ambassador reintenta con otro worker diferente. |
| **Auto-reintento en Coordinator** | Fase 2 automatica: re-despacha chunks que fallaron tras agotar reintentos del Ambassador. |
| **Estado partial + /retry-failed** | Permite recuperar chunks fallidos sin repetir todo el procesamiento. |
| **Persistencia del registro** | `workers_registry.json` sobrevive reinicios del Ambassador. |
| **Health check pre-vuelo** | `POST /start` verifica salud de workers antes de despachar, excluyendo los que no responden. |
| **Flask threaded=True** | Permite que el Ambassador atienda multiples chunks en paralelo sin encolarlos. |
| **Lectura en bloques de 64 MB** | Evita OOM al procesar chunks de gigabytes. |

---

*Sistemas Distribuidos — Proyecto Final*
