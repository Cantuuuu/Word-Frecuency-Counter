# Contador Distribuido de Frecuencia de Palabras
## Ambassador y Circuit Breaker  
## Sistemas Distribuidos — Proyecto Final

Sistema distribuido que cuenta la frecuencia de palabras en un corpus de gran tamaño (1–5 GB) dividiendo el archivo por **rangos de bytes (offsets)** entre varios workers, coordinados por un nodo central. Toda la comunicación pasa por un **Ambassador** que aplica **Circuit Breaker**, timeouts, reintentos y redistribución de carga ante fallos.

- **Coordinator** (`:4999`): no lee el contenido del corpus; solo obtiene su tamaño en bytes, calcula offsets `(inicio, fin)`, despacha los fragmentos en paralelo y combina los resultados parciales.
- **Ambassador** (`:5005`): único punto de contacto del Coordinator. Selecciona el worker disponible (Round-Robin), aplica timeout configurable, reintenta con rotación y mantiene un Circuit Breaker por worker (CLOSED / OPEN / HALF_OPEN). Registra en consola: worker seleccionado, rango de bytes, tiempo de respuesta, intentos y estado final.
- **Workers** (`:5001`): cada uno recibe `(offset_inicio, offset_fin)`, lee **únicamente su fragmento** de su copia local del archivo, cuenta frecuencias y responde en JSON.

Cada nodo tiene su propia copia del corpus: por la red solo viajan offsets (metadatos) y diccionarios de frecuencia (JSON), nunca el texto.

> **Nota sobre la estructura:** los archivos que el enunciado ubica en `programa/` (`coordinator.py`, `ambassador.py`, `circuit_breaker.py`, `worker.py`, `ground_truth.py`) están en la **raíz del repositorio**. El informe y los diagramas se encuentran en el documento entregado.

---

## 1. Corpus de prueba (generar o descargar)

El sistema funciona con cualquier archivo de texto plano (UTF-8). Para los experimentos usamos un dump de Wikipedia en español (`wiki_es.txt`, ~4.92 GB).

**Opción A — Descargar texto real:** descargar un dump generado con el contenido total de la Wikipedia en Español [drive](https://drive.google.com/file/d/1Wdggyu9C4--9clAlHaO45mw1PmubIOPK/view?usp=sharing), en este archivo ya se encuentra el texto concatenado de varias páginas, con ~5 GB de tamaño. Solo hay que descargarlo y colocarlo en la raíz del proyecto (o en cualquier ruta, pero actualizar la referencia en el Coordinator y montar esa ruta en los workers).

**Opción B — Generar un corpus del tamaño deseado:** concatenar un texto base consigo mismo hasta superar 5 GB:

```powershell
# Windows (PowerShell)
1..50 | ForEach-Object { Get-Content base.txt -Raw | Add-Content wiki_es.txt -NoNewline }
```

```bash
# Linux / macOS
for i in $(seq 50); do cat base.txt >> wiki_es.txt; done
```

**No se necesitan 5 archivos distintos**: el Coordinator limita el corpus lógico a los primeros N GB con el parámetro `corpus_gb`, así que un único archivo de ~5 GB sirve para los cinco experimentos.

**Requisito imprescindible:** el archivo debe ser **idéntico byte a byte en todos los nodos** (el reparto es por offsets de byte). Verificar el tamaño exacto en cada nodo:

```powershell
(Get-Item wiki_es.txt).Length     # Windows
```
```bash
stat -c%s wiki_es.txt             # Linux
```

---

## 2. Instalación de dependencias

**Con Docker (recomendado):** solo se necesita [Docker Desktop](https://www.docker.com/products/docker-desktop/). Las imágenes instalan sus dependencias al construirse.

**Sin Docker (ejecución directa con Python 3.11+):**

```bash
pip install -r requirements.txt    # flask, requests
```

---

## 3. Ground Truth secuencial

Conteo de referencia con un solo proceso, usado para verificar correctitud y medir el speedup:

```bash
python ground_truth.py                       # usa wiki_es.txt en la raíz
WIKI_PATH=otro_archivo.txt python ground_truth.py
```

Imprime el tiempo secuencial (`T_seq`), el total de palabras y el top de frecuencias.

El Coordinator también puede ejecutar el ground truth automáticamente al final de cada corrida (activado por defecto en `POST /start`) y comparar ambos conteos palabra por palabra: el campo `correcto` de `/result` indica si el resultado distribuido es **idéntico** al secuencial.

---

## 4. Iniciar los workers

### Todo en una máquina (docker compose)

```bash
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter
git checkout coordinator

# Colocar wiki_es.txt en la raíz del proyecto

docker compose up --build    # levanta Ambassador + Coordinator + 1 worker local
```

Para cumplir el mínimo de **3 workers** en una sola máquina, levantar workers adicionales (cada uno es un contenedor independiente; cambiar puerto de host y `WORKER_URL`):

```powershell
docker build -f Dockerfile.worker -t wordcounter-worker .

docker run -d --rm `
  -p 5002:5001 `
  -e AMBASSADOR_URL=http://host.docker.internal:5005 `
  -e WORKER_URL=http://host.docker.internal:5002 `
  -v ${PWD}\wiki_es.txt:/app/data/input.txt:ro `
  --name worker_5002 wordcounter-worker
# repetir con 5003, 5004, ... para más workers
```

### Workers en otras computadoras (misma red WiFi/LAN)

En cada laptop remota (con su copia local del corpus):

```powershell
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter && git checkout coordinator
docker build -f Dockerfile.worker -t wordcounter-worker .

docker run --rm `
  -p 5002:5001 `
  -e AMBASSADOR_URL=http://<IP_DEL_COORDINATOR>:5005 `
  -e WORKER_URL=http://<IP_DE_ESTA_LAPTOP>:5002 `
  -v <RUTA_ABSOLUTA_DEL_CORPUS>:/app/data/input.txt:ro `
  wordcounter-worker
```

Los workers se **auto-registran** con el Ambassador (no hay que configurar IPs en el nodo central). Verificar que todos estén conectados:

```bash
curl http://localhost:5005/workers/status   # workers registrados + estado de sus Circuit Breakers
curl http://localhost:5005/workers/health   # reachable: true y file_accessible: true en todos
```

---

## 5. Ejecutar el coordinador con un corpus

```bash
# Corrida completa con ground truth (verifica correctitud y calcula speedup)
curl -X POST http://localhost:4999/start

# Limitar el corpus lógico a N GB (mismo archivo, experimentos de 1-5 GB)
curl -X POST http://localhost:4999/start -H "Content-Type: application/json" -d '{"corpus_gb": 1}'

# Sin ground truth (solo conteo distribuido)
curl -X POST http://localhost:4999/start -H "Content-Type: application/json" -d '{"ground_truth": false}'

# Monitorear y obtener resultados (también se muestran en consola)
curl http://localhost:4999/status
curl http://localhost:4999/result
```

O usar el **dashboard web** en `http://localhost:4999`: iniciar/resetear corridas, elegir tamaño de corpus, ver workers y Circuit Breakers en vivo, top de palabras, speedup, historial y exportar el conteo completo a CSV.

Para re-ejecutar:

```bash
curl -X POST http://localhost:4999/reset
curl -X POST http://localhost:5005/workers/reset-cbs   # opcional: resetear Circuit Breakers
curl -X POST http://localhost:4999/start
```

---

## 6. Casos de prueba con fallos de workers

El fallo se induce manualmente durante una corrida. Formas de inducirlo:

- Detener el contenedor del worker: `docker stop worker_5002` (o Ctrl-C en su terminal).
- Levantar un worker que siempre falla: `docker run ... -e FAIL_MODE=true ...` (responde HTTP 500).
- Levantar un worker lento: `docker run ... -e DELAY=<segundos> ...`.

Observar la reacción del sistema en la consola del Ambassador (intentos, rotación de worker, estado del CB) y en:

```bash
curl http://localhost:5005/workers/status   # campo detalle.<worker>.state: CLOSED / OPEN / HALF_OPEN
```

### Caso 1 — Un worker cae a mitad del corpus de 1 GB

```bash
curl -X POST http://localhost:4999/start -H "Content-Type: application/json" -d '{"corpus_gb": 1}'
# cuando el procesamiento esté ~50%:
docker stop worker_5002
```

El Circuit Breaker del worker pasa a OPEN tras 2 fallos consecutivos y su fragmento se **redistribuye automáticamente** a otro worker activo. La corrida termina al 100 % con `correcto: true`.

### Caso 2 — Dos workers caen simultáneamente durante 3 GB

```bash
curl -X POST http://localhost:4999/start -H "Content-Type: application/json" -d '{"corpus_gb": 3}'
# a mitad del procesamiento:
docker stop worker_5002 worker_5003
```

Los fragmentos de ambos caídos se reparten entre los workers restantes; ningún fragmento se pierde.

### Caso 3 — Un worker cae y se recupera (OPEN → HALF_OPEN → CLOSED)

```bash
curl -X POST http://localhost:4999/start -H "Content-Type: application/json" -d '{"corpus_gb": 1}'
docker stop worker_5002          # CB → OPEN tras 2 fallos
# ... volver a levantarlo con el mismo docker run de la sección 4 ...
```

Tras `RESET_TIMEOUT` (10 s) el CB pasa a HALF_OPEN, envía una petición de prueba y, si responde, vuelve a CLOSED, reincorporando al worker al pool. La transición es visible en `GET /workers/status`.

Si **todos** los workers fallan, el Coordinator no pierde los fragmentos: los reintenta cada 10 s y reporta el estado de forma controlada hasta que algún worker vuelva.

---

## 7. Resultados de los experimentos

Ejecutados con **4 workers**, mismo archivo en todos los nodos (la fila de 5 GB corresponde al archivo completo, 4.92 GB). `Speedup = T_secuencial / T_distribuido`; Correctitud ✓ = conteo distribuido idéntico al ground truth, palabra por palabra.

| Corpus | T. Secuencial (s) | T. Distribuido (s) | Speedup | Correctitud | Palabras únicas |
|--------|-------------------|--------------------|---------|-------------|-----------------|
| 1 GB   | 72.03             | 36.44              | 1.98x   | ✓           | 1,173,838       |
| 2 GB   | 137.85            | 66.16              | 2.08x   | ✓           | 1,863,253       |
| 3 GB   | 209.96            | 102.69             | 2.04x   | ✓           | 2,468,574       |
| 4 GB   | 322.63            | 128.30             | 2.51x   | ✓           | 3,056,908       |
| 5 GB (archivo completo, 4.92 GB) | 716.62 | 166.80 | 4.30x | ✓ | 3,518,471 |

El análisis completo, las gráficas de speedup y los resultados de los casos de fallo están en el informe.

---

## Pruebas unitarias

```bash
python -m pytest test_circuit_breaker.py -v   # 23 tests del Circuit Breaker
python -m pytest test_components.py -v        # tests de integración
```

---

## Solución de problemas

**Worker remoto no aparece en `/workers/status`** — casi siempre es el firewall. Abrir puertos (como Administrador en Windows):

```
netsh advfirewall firewall add rule name="Worker WordCounter" dir=in action=allow protocol=TCP localport=5002
netsh advfirewall firewall add rule name="Ambassador WordCounter" dir=in action=allow protocol=TCP localport=5005
```

**`/workers/health` muestra `file_accessible: false`** — el corpus no está montado: verificar que la ruta del `-v` sea absoluta y el archivo exista.

**Docker no puede descargar `python:3.11-slim`** — en una PC con internet: `docker save wordcounter-worker -o worker_image.tar`; copiar a la laptop y `docker load -i worker_image.tar`.

---

## Estructura del proyecto

```
WordCounter/
  coordinator.py           # Orquestador: offsets, despacho paralelo, reintentos, combinación
  ambassador.py            # Proxy: registro dinámico, Round-Robin, Circuit Breakers, reintentos
  worker.py                # Conteo de palabras por rango de bytes (lee solo su fragmento)
  circuit_breaker.py       # Circuit Breaker thread-safe (CLOSED / OPEN / HALF_OPEN)
  ground_truth.py          # Conteo secuencial de referencia
  config.py                # Configuración centralizada
  docker-compose.yml       # Ambassador + Worker local + Coordinator
  Dockerfile.ambassador / Dockerfile.coordinator / Dockerfile.worker
  requirements.txt         # flask, requests
  test_circuit_breaker.py  # Tests unitarios del CB
  test_components.py       # Tests de integración
  templates/dashboard.html # Dashboard web (polling c/3s)
```

---

## Configuración (config.py)

| Parámetro | Valor | Descripción |
|-----------|-------|-------------|
| `FAIL_MAX` | 2 | Fallos consecutivos para abrir el Circuit Breaker |
| `RESET_TIMEOUT` | 10 s | Segundos en OPEN antes de pasar a HALF_OPEN |
| `MAX_RETRIES` | 3 | Intentos por chunk en el Ambassador (3 = 2 reintentos) |
| `REQUEST_TIMEOUT` | 600 s | Timeout HTTP por chunk |
| `BUFFER_SIZE` | 64 MB | Bloque de lectura en el worker |
| `BOUNDARY_BUF` | 512 B | Bytes extra para palabras en límites de chunk |

---

*Sistemas Distribuidos — Proyecto Final*
