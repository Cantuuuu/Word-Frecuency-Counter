# Distributed Word-Frequency Counter

Un sistema distribuido para contar la frecuencia de palabras en archivos de texto masivos (Wikipedia en español, 5.2 GB) utilizando una arquitectura de microservicios con balanceo de carga, tolerancia a fallos y reintentos automaticos.

## Arquitectura del Sistema

El sistema distribuye el procesamiento en multiples computadoras (nodos), evitando enviar el archivo pesado por red. Solo se transmiten metadatos (rangos de bytes y diccionarios de frecuencia en JSON).

| Componente | Puerto | Responsabilidad |
|------------|--------|-----------------|
| **Coordinator** | 4999 | Divide el archivo en chunks (rangos de bytes), los despacha en paralelo al Ambassador y combina los resultados. |
| **Ambassador** | 5005 | Proxy inteligente: balancea carga con Round-Robin, gestiona Circuit Breakers por worker y aplica politica de reintentos con rotacion. |
| **Workers** (x3) | 5001 | Servidores Flask que reciben un rango de bytes, leen su copia local del archivo, extraen palabras con regex y retornan el conteo. |

### Flujo de datos

```
Coordinator ──POST /dispatch──> Ambassador ──POST /count──> Worker
                                    │                         │
                                    │   (Round-Robin +        │  seek(start)
                                    │    Circuit Breaker)     │  read(end - start)
                                    │                         │  regex + Counter
                                    │                         │
                                    <────── JSON {word:count} <
```

> **Dato clave:** El archivo `wiki_es.txt` reside localmente en cada nodo. El sistema solo intercambia offsets de inicio/fin y diccionarios JSON por red.

## Tecnologias

* **Lenguaje:** Python 3.11
* **Web Framework:** Flask (servidores REST multi-hilo)
* **Comunicacion HTTP:** Libreria `requests`
* **Infraestructura:** Docker y Docker Compose

---

## Requisitos Previos

1. [Docker Desktop](https://www.docker.com/products/docker-desktop/) instalado en cada computadora.
2. El archivo `wiki_es.txt` (5.2 GB) en la raiz del proyecto en **cada** computadora.
3. Todas las computadoras conectadas a la **misma red WiFi/LAN**.

---

## Ejecucion Local (una sola maquina)

Para pruebas rapidas sin necesidad de red:

```bash
# 1. Clonar el repositorio
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter

# 2. Colocar wiki_es.txt en la raiz del proyecto

# 3. Crear el archivo .env para modo local
cp .env.example .env
# Editar .env: descomentar las lineas de "Modo local" y comentar las de "Modo red"

# 4. Levantar todo (Ambassador + Worker local + Coordinator)
docker-compose up --build

# 5. Detener
docker-compose down
```

---

## Despliegue en Red (multiples computadoras)

### Topologia

```
  TU PC (Coordinator + Ambassador)
  ┌─────────────────────────────────┐
  │  docker-compose up --build      │
  │  - coordinator :4999            │
  │  - ambassador  :5005            │
  └──────────┬──────────────────────┘
             │  red WiFi/LAN
     ┌───────┼───────┐
     │       │       │
     v       v       v
  Laptop 1  Laptop 2  Laptop 3
  Worker 1  Worker 2   Worker 3
  :5001     :5001      :5001
```

- **Tu PC** corre el Coordinator y el Ambassador (via `docker-compose`).
- **Cada laptop remota** corre unicamente un Worker (via `docker run`).
- Cada maquina tiene su propia copia del archivo `wiki_es.txt`.

---

### Paso 1: Preparar cada laptop remota (Workers)

Repetir en cada una de las 3 laptops:

```bash
# 1. Clonar el repositorio
git clone https://github.com/Cantuuuu/Word-Frecuency-Counter.git
cd Word-Frecuency-Counter

# 2. Colocar wiki_es.txt en la raiz del proyecto
#    (copiar via USB o transferencia en red)

# 3. Construir la imagen del Worker
docker build -f Dockerfile.worker -t wc-worker .

# 4. Ejecutar el Worker
#    Reemplazar WORKER_ID con: worker_1, worker_2 o worker_3
docker run --rm -p 5001:5001 \
  -e WORKER_ID=worker_1 \
  -v "./wiki_es.txt:/app/data/input.txt:ro" \
  wc-worker
```

> **Verificar que funciona:** Desde la misma laptop, abrir un navegador y visitar `http://localhost:5001/health`. Debe responder `{"status": "ok", ...}`.

---

### Paso 2: Obtener la IP de cada laptop worker

Cada laptop necesita saber su IP en la red local:

**Windows:**
```
ipconfig
```
Buscar la seccion del adaptador Wi-Fi y anotar la linea **"IPv4 Address"** (ej. `192.168.1.101`).

**macOS:**
```
ifconfig en0
```
Buscar la linea **"inet"** (ej. `192.168.1.103`).

**Verificar conectividad** desde tu PC:
```
ping 192.168.1.101
```

---

### Paso 3: Configurar tu PC (Coordinator + Ambassador)

```bash
# 1. Ir al directorio del proyecto
cd Word-Frecuency-Counter

# 2. Crear el archivo .env con las IPs reales de los workers
cp .env.example .env
```

Editar `.env` con las IPs obtenidas en el paso anterior:

```env
WORKER_1_URL=http://192.168.1.101:5001
WORKER_2_URL=http://192.168.1.102:5001
WORKER_3_URL=http://192.168.1.103:5001
```

```bash
# 3. Levantar Coordinator + Ambassador
docker-compose up --build
```

El Coordinator hara un health check al Ambassador, dividira el archivo en 3 chunks y los despachara en paralelo. Al terminar, imprime las 20 palabras mas frecuentes y el speedup vs. el conteo secuencial.

---

### Solucion de problemas comunes

#### El Ambassador no puede conectar con un Worker

**Causa mas probable: Firewall.** Por defecto, Windows y macOS bloquean conexiones entrantes.

**Windows** — abrir puerto 5001 (ejecutar como Administrador):
```
netsh advfirewall firewall add rule name="Worker WordCounter" dir=in action=allow protocol=TCP localport=5001
```

Para eliminar la regla despues de la demo:
```
netsh advfirewall firewall delete rule name="Worker WordCounter"
```

**macOS** — en Ajustes del Sistema > Red > Firewall: desactivar temporalmente o permitir conexiones entrantes para Docker.

#### Verificar que el Worker es accesible desde tu PC

Desde tu PC, con la IP del worker:
```
curl http://192.168.1.101:5001/health
```
Debe responder: `{"status": "ok", "worker_id": "worker_1", ...}`

Si no responde: revisar firewall, verificar que Docker esta corriendo y que el `docker run` sigue activo.

#### Las IPs cambiaron

Las IPs asignadas por WiFi son dinamicas. Si una laptop se desconecta y reconecta, puede obtener una IP diferente. Obtener las IPs justo antes de iniciar la demo.

#### Error de bind mount en Windows

Si Docker no puede montar `wiki_es.txt`, usar la ruta absoluta completa:
```bash
docker run --rm -p 5001:5001 \
  -e WORKER_ID=worker_1 \
  -v "C:/Users/tu_usuario/Word-Frecuency-Counter/wiki_es.txt:/app/data/input.txt:ro" \
  wc-worker
```

---

## Pruebas y Validacion

### Test de componentes (Mock del Coordinator)
Envia dos bloques de prueba para verificar el flujo Ambassador-Worker sin necesidad del Coordinator completo:
```bash
python test_components.py
```

### Ground Truth (Linea base secuencial)
Calcula el conteo secuencial en un solo hilo para medir el speedup del sistema distribuido:
```bash
python ground_truth.py
```

---

## Decisiones de Diseño

| Decision | Razon |
|----------|-------|
| **Puerto 5005 para el Ambassador** | Evita conflicto con AirPlay Receiver de macOS (que usa el puerto 5000). |
| **Timeout de 20 segundos** | Permite que workers con discos lentos o virtualizacion Docker completen la lectura del archivo. |
| **Archivo local en cada nodo** | Evita transferir 5.2 GB por red; solo se intercambian metadatos JSON (KB). |
| **Bind mount (no COPY)** | El archivo no entra en la imagen Docker; la imagen pesa ~150 MB en vez de 5.3 GB. |
| **Round-Robin con Circuit Breaker** | Distribuye carga equitativamente y excluye workers caidos sin esperar timeout. |
| **Reintentos con rotacion** | Si un worker falla, el Ambassador reintenta con otro worker diferente (hasta 3 intentos). |
| **Flask threaded=True** | Permite que el Ambassador atienda multiples chunks en paralelo sin encolarlos. |

---

## Estructura del Proyecto

```
WordCounter/
  ambassador.py            # Servidor Ambassador (proxy + balanceador)
  circuit_breaker_mock.py  # Implementacion del Circuit Breaker (thread-safe)
  config.py                # Configuracion centralizada (IPs, puertos, timeouts)
  coordinator.py           # Orquestador: divide, despacha, combina
  worker.py                # Servidor Worker (conteo de palabras)
  docker-compose.yml       # Orquestacion Docker (Ambassador + Worker local + Coordinator)
  Dockerfile.ambassador    # Imagen Docker del Ambassador
  Dockerfile.coordinator   # Imagen Docker del Coordinator
  Dockerfile.worker        # Imagen Docker del Worker
  .env.example             # Plantilla de variables de entorno
  requierements.txt        # Dependencias Python (flask, requests)
  ground_truth.py          # Conteo secuencial para benchmark
  test_components.py       # Tests de integracion mock
```

---

*Sistemas Distribuidos*

*Proyecto Final - Word Frequency Counter*

