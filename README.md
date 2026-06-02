# Distributed Word-Frequency Counter

Sistema distribuido para contar la frecuencia de palabras en archivos de texto masivos (~5.2 GB) utilizando una arquitectura de microservicios con Ambassador, Circuit Breaker y reintentos automaticos.

Cada nodo tiene su propia copia local del archivo. Solo se transmiten rangos de bytes y diccionarios de frecuencia por red.

---

## Requisitos

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) en cada computadora
- El archivo `wiki_es.txt` en cada nodo participante
- Para modo red: todas las computadoras en la misma red WiFi/LAN

---

## Ejecucion Local (una sola maquina)

```bash
# 1. Clonar el repositorio
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter
git checkout coordinator

# 2. Colocar wiki_es.txt en la raiz del proyecto

# 3. Levantar todo (Ambassador + Worker local + Coordinator)
docker compose up --build

# 4. Abrir el dashboard en el navegador
#    http://localhost:4999

# 5. O lanzar desde terminal
curl -X POST http://localhost:4999/start

# 6. Monitorear
curl http://localhost:4999/status
curl http://localhost:4999/result

# 7. Detener
docker compose down
```

El `docker-compose.yml` incluye Ambassador, Coordinator y un `worker_local` que se auto-registra. No se necesita archivo `.env`.

---

## Dashboard Web

Accesible en `http://localhost:4999`. Permite:

- Iniciar procesamiento (con o sin ground truth)
- Resetear el estado
- Ver workers registrados, estado de Circuit Breakers y workers ocupados
- Monitorear chunks en proceso y chunks lentos (>60s)
- Ver resultados: top 20 palabras, speedup, historial de ejecuciones

El dashboard actualiza automaticamente cada 3 segundos.

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
              Laptop A        Laptop B
              Worker A        Worker B
              :5002           :5002
```

### Paso 1: Tu PC — levantar los servicios principales

```bash
docker compose up --build
```

Obtener tu IP local:

```bash
# Windows
ipconfig    # Buscar "Adaptador Wi-Fi" → "Direccion IPv4"

# macOS/Linux
ifconfig en0   # Buscar "inet"
```

### Paso 2: Cada laptop remota — levantar el worker

```bash
# 1. Clonar y construir
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter && git checkout coordinator
docker build -f Dockerfile.worker -t wordcounter-worker .
```

Ejecutar el worker (reemplazar `<IP_TU_PC>`, `<IP_ESTA_LAPTOP>` y `<RUTA_ARCHIVO>`):

**Windows (PowerShell):**
```powershell
docker run --rm `
  -p 5002:5001 `
  -e AMBASSADOR_URL=http://<IP_TU_PC>:5005 `
  -e WORKER_URL=http://<IP_ESTA_LAPTOP>:5002 `
  -v <RUTA_ARCHIVO>:/app/data/input.txt:ro `
  wordcounter-worker
```

**Linux/macOS:**
```bash
docker run --rm \
  -p 5002:5001 \
  -e AMBASSADOR_URL=http://<IP_TU_PC>:5005 \
  -e WORKER_URL=http://<IP_ESTA_LAPTOP>:5002 \
  -v <RUTA_ARCHIVO>:/app/data/input.txt:ro \
  wordcounter-worker
```

Los workers se auto-registran con el Ambassador. No se necesita configurar IPs manualmente en tu PC.

#### Varios workers en una misma máquina

Cada worker es un contenedor independiente. Para levantar N workers en una sola laptop,
corre N veces `docker run` cambiando el **puerto de host** y el **WORKER_URL** (el puerto
interno del contenedor siempre es 5001). El Ambassador les asigna IDs distintos
(`worker_01`, `worker_02`, ...) por URL. Ejemplo de 3 workers en una Linux con IP `<IP_LAPTOP>`:

```bash
for p in 5002 5003 5004; do
  docker run -d --rm \
    -p $p:5001 \
    -e AMBASSADOR_URL=http://<IP_TU_PC>:5005 \
    -e WORKER_URL=http://<IP_LAPTOP>:$p \
    -v <RUTA_ARCHIVO>:/app/data/input.txt:ro \
    --name worker_$p \
    wordcounter-worker
done
```

El número de chunks lo decide el Coordinator automáticamente: `num_chunks = workers sanos`.

### Paso 3: Verificar conexion

```bash
# Ver workers registrados y estado de Circuit Breakers
curl http://localhost:5005/workers/status

# Health check de todos los workers
curl http://localhost:5005/workers/health
```

Todos los workers deben aparecer con `"reachable": true` y `"file_accessible": true`.

### Paso 4: Lanzar el procesamiento

```bash
# Con ground truth (compara contra conteo secuencial)
curl -X POST http://localhost:4999/start

# Sin ground truth (mas rapido)
curl -X POST http://localhost:4999/start \
  -H "Content-Type: application/json" \
  -d '{"ground_truth": false}'

# Procesar solo los primeros N GB del archivo (control de tamano centralizado:
# un unico archivo sirve para experimentar con 1, 2, 3, 4, 5 GB)
curl -X POST http://localhost:4999/start \
  -H "Content-Type: application/json" \
  -d '{"corpus_gb": 1}'
```

O usar el dashboard en `http://localhost:4999`.

> Para correr los experimentos de rendimiento (1-5 GB) y los casos de fallo y
> guardarlos en CSV, ver `experimentos/README.md`.

---

## Re-ejecucion y Reset

```bash
# 1. Resetear el coordinator
curl -X POST http://localhost:4999/reset

# 2. (Opcional) Resetear Circuit Breakers si hubo fallos previos
curl -X POST http://localhost:5005/workers/reset-cbs

# 3. Lanzar de nuevo
curl -X POST http://localhost:4999/start
```

---

## Solucion de Problemas

### Worker remoto no aparece en /workers/status

**Causa mas probable: Firewall.** Abrir puertos (ejecutar como Administrador en Windows):

```
netsh advfirewall firewall add rule name="Worker WordCounter" dir=in action=allow protocol=TCP localport=5002
netsh advfirewall firewall add rule name="Ambassador WordCounter" dir=in action=allow protocol=TCP localport=5005
```

### /workers/health muestra `file_accessible: false`

El archivo no esta montado correctamente. Verificar que la ruta en `-v` es absoluta y que el archivo existe.

### Docker no puede descargar `python:3.11-slim`

```bash
# En una PC con internet:
docker save wordcounter-worker -o worker_image.tar
# Copiar a la laptop y cargar:
docker load -i worker_image.tar
```

---

## Pruebas

```bash
# Tests unitarios del Circuit Breaker (23 tests)
python -m pytest test_circuit_breaker.py -v

# Ground truth — conteo secuencial de referencia
python ground_truth.py
```

---

## Estructura del Proyecto

```
WordCounter/
  coordinator.py           # Orquestador: divide, despacha, reintenta, combina
  ambassador.py            # Proxy: registro dinamico, Round-Robin, CB, reintentos
  worker.py                # Conteo de palabras por rango de bytes
  circuit_breaker.py       # Circuit Breaker thread-safe (3 estados)
  config.py                # Configuracion centralizada
  docker-compose.yml       # Ambassador + Worker local + Coordinator
  Dockerfile.ambassador
  Dockerfile.coordinator
  Dockerfile.worker
  requirements.txt         # flask, requests
  ground_truth.py          # Conteo secuencial para benchmark
  test_circuit_breaker.py  # Tests unitarios del CB
  test_components.py       # Tests de integracion
  templates/
    dashboard.html         # Dashboard web (polling c/3s)
  docs/
    diagramas.md           # Diagramas Mermaid del sistema
```

---

## Configuracion (config.py)

| Parametro | Valor | Descripcion |
|-----------|-------|-------------|
| `FAIL_MAX` | 2 | Fallos consecutivos para abrir el Circuit Breaker |
| `RESET_TIMEOUT` | 10s | Segundos en OPEN antes de pasar a HALF_OPEN |
| `MAX_RETRIES` | 3 | Intentos por chunk en el Ambassador (3 = 2 reintentos) |
| `REQUEST_TIMEOUT` | 600s | Timeout HTTP por chunk (10 min) |
| `BUFFER_SIZE` | 64 MB | Bloque de lectura en el worker |
| `BOUNDARY_BUF` | 512 B | Bytes extra para palabras en limites de chunk |

---

*Sistemas Distribuidos — Proyecto Final*
