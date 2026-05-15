# Diagramas del Sistema WordCounter

## 1. Diagrama de Arquitectura

```mermaid
graph LR
    subgraph "Máquina Coordinador (tu laptop)"
        U([Usuario<br>curl / browser])
        CO["Coordinator<br>Puerto 4999<br>(Docker)"]
        AM["Ambassador<br>Puerto 5005<br>(Docker)"]
        CB["Circuit Breakers<br>(en memoria, 1 por worker)"]
        REG[("workers_registry.json<br>persistencia")]
        WL["Worker Local<br>Puerto 5001<br>(Docker)"]
        DL[("wiki_es.txt<br>5.2 GB<br>bind mount")]
        DC[("wiki_es.txt<br>5.2 GB<br>bind mount")]

        AM <--> CB
        AM --- REG
        WL --- DL
        CO --- DC
    end

    subgraph "Nodo Worker Remoto 1"
        W1["Worker<br>Puerto 5001<br>(Docker)"]
        D1[("wiki_es.txt<br>5.2 GB<br>bind mount")]
        W1 --- D1
    end

    subgraph "Nodo Worker Remoto 2"
        W2["Worker<br>Puerto 5001<br>(Docker)"]
        D2[("wiki_es.txt<br>5.2 GB<br>bind mount")]
        W2 --- D2
    end

    U -->|"GET / (dashboard)<br>POST /start<br>POST /reset<br>GET /status<br>GET /result"| CO
    U -->|"GET /workers/status<br>GET /workers/health<br>POST /workers/reset-cbs<br>DELETE /workers/id"| AM
    CO -->|"POST /dispatch<br>GET /workers/status<br>GET /workers/health"| AM
    AM -->|"POST /count<br>GET /health"| WL
    AM -->|"POST /count<br>GET /health"| W1
    AM -->|"POST /count<br>GET /health"| W2
    WL -.->|"POST /register<br>(reintento indefinido c/15s<br>+ monitoreo continuo)"| AM
    W1 -.->|"POST /register<br>(reintento indefinido c/15s<br>+ monitoreo continuo)"| AM
    W2 -.->|"POST /register<br>(reintento indefinido c/15s<br>+ monitoreo continuo)"| AM

    style CO fill:#4A90D9,color:#fff
    style AM fill:#E67E22,color:#fff
    style WL fill:#27AE60,color:#fff
    style W1 fill:#27AE60,color:#fff
    style W2 fill:#27AE60,color:#fff
```

---

## 2. Diagrama de Secuencia

```mermaid
sequenceDiagram
    participant U as Usuario
    participant CO as Coordinator :4999
    participant AM as Ambassador :5005
    participant W1 as Worker 1 :5001
    participant W2 as Worker 2 :5001

    Note over W1,W2: Fase 0 — Auto-registro (reintentos indefinidos, c/15s)

    W1->>AM: POST /register {url}
    AM->>AM: Crea CircuitBreaker(w1, fail_max=2, reset_timeout=10)
    AM->>AM: _save_registry() → workers_registry.json
    AM-->>W1: 200 OK {worker_id: "worker_01", total_workers: 1}
    W2->>AM: POST /register {url}
    AM->>AM: Crea CircuitBreaker(w2, fail_max=2, reset_timeout=10)
    AM->>AM: _save_registry() → workers_registry.json
    AM-->>W2: 200 OK {worker_id: "worker_02", total_workers: 2}

    Note over W1,W2: Fase 0b — Monitoreo continuo (c/15s)
    loop Cada 15s tras registro
        W1->>AM: GET /health (timeout 3s)
        AM-->>W1: 200 {status: ok, service: ambassador}
        Note over W1: Si falla → _registered.clear() → vuelve a Fase 0
    end

    Note over U,W2: Fase 1 — Inicio del procesamiento

    U->>CO: POST /start {ground_truth: true}
    CO->>CO: Valida archivo (os.path.isfile + getsize > 0)
    CO->>AM: GET /workers/status
    AM-->>CO: {registered: [w1, w2]}
    CO->>AM: GET /workers/health
    AM->>W1: GET /health (timeout 3s)
    AM->>W2: GET /health (timeout 3s)
    W1-->>AM: {file_accessible: true, uptime_s: ...}
    W2-->>AM: {file_accessible: true, uptime_s: ...}
    AM-->>CO: {listos: [w1, w2], n_listos: 2}
    CO->>CO: Excluye workers no listos (num_chunks = len(listos))
    CO-->>U: 200 {status: started, workers: [w1, w2], num_chunks: 2}

    Note over CO: Hilo daemon inicia _run_processing()
    CO->>CO: _calcular_chunks(file, 2) → os.path.getsize() → [(0, 2.6GB), (2.6GB, 5.2GB)]

    Note over CO,W2: Fase 2 — Despacho paralelo (ThreadPoolExecutor)

    par Chunk 0
        CO->>AM: POST /dispatch {chunk_id:0, inicio:0, fin:2.6GB}
        AM->>AM: _seleccionar_worker() → Round-Robin + CB allow_request() + busy check
        AM->>W1: POST /count {start:0, end:2.6GB}
        W1->>W1: _ajustar_inicio(start) → alinea a frontera de palabra
        W1->>W1: read en bloques de 64MB + BOUNDARY_BUF(512B) en límites
        W1->>W1: re.findall(r"\b\w+\b", chunk.lower()) → Counter
        W1-->>AM: {worker_id:w1, result:{word:count,...}}
        AM->>AM: cb_w1.record_success() → fail_count=0
        AM-->>CO: {chunk_id:0, status:ok, result:{...}}
    and Chunk 1
        CO->>AM: POST /dispatch {chunk_id:1, inicio:2.6GB, fin:5.2GB}
        AM->>AM: _seleccionar_worker() → w2
        AM->>W2: POST /count {start:2.6GB, end:5.2GB}
        W2->>W2: _ajustar_inicio(start) → alinea a frontera de palabra
        W2->>W2: read bloques 64MB + regex + Counter
        W2-->>AM: {worker_id:w2, result:{...}}
        AM->>AM: cb_w2.record_success()
        AM-->>CO: {chunk_id:1, status:ok, result:{...}}
    end

    Note over CO: Fase 3 — Agregación

    CO->>CO: total_counter.update() por cada chunk exitoso
    CO->>CO: estado → "done" (todos los chunks completados)

    Note over CO: Ground truth secuencial (opcional)

    CO->>CO: Lee archivo línea por línea → re.findall(r"\b\w+\b") → Counter secuencial
    CO->>CO: Calcula speedup = t_seq / t_dist
    CO->>CO: Guarda en _history (máx 5 entradas)
```

---

## 3. Diagrama de Secuencia con Circuit Breaker (escenario de fallo)

```mermaid
sequenceDiagram
    participant CO as Coordinator :4999
    participant AM as Ambassador :5005
    participant CB1 as CB: Worker 1
    participant CB2 as CB: Worker 2
    participant CB3 as CB: Worker 3
    participant W1 as Worker 1
    participant W2 as Worker 2
    participant W3 as Worker 3

    Note over CB1,CB3: Todos los CB inician en CLOSED (fail_count=0, fail_max=2)

    CO->>AM: POST /dispatch {chunk_id:0, inicio:0, fin:1.7GB, max_retries:3}

    Note over AM: Intento 1/3 — ya_fallaron = {}

    AM->>AM: _seleccionar_worker() → w1
    AM->>CB1: allow_request()?
    CB1-->>AM: true (CLOSED)
    AM->>AM: _busy_workers.add(w1)
    AM->>W1: POST /count {start:0, end:1.7GB} (timeout=600s)
    W1--xAM: Timeout / ConnectionError
    AM->>CB1: record_failure() → fail_count=1
    AM->>AM: _busy_workers.discard(w1) (finally)
    AM->>AM: ya_fallaron.add(w1)

    Note over AM: Intento 2/3 — ya_fallaron = {w1}

    AM->>AM: _seleccionar_worker(excluidos={w1}) → w2
    AM->>CB2: allow_request()?
    CB2-->>AM: true (CLOSED)
    AM->>AM: _busy_workers.add(w2)
    AM->>W2: POST /count {start:0, end:1.7GB}
    W2--xAM: ConnectionError
    AM->>CB2: record_failure() → fail_count=1
    AM->>AM: _busy_workers.discard(w2) (finally)
    AM->>AM: ya_fallaron.add(w2)

    Note over AM: Intento 3/3 — ya_fallaron = {w1, w2}

    AM->>AM: _seleccionar_worker(excluidos={w1, w2}) → w3
    AM->>CB3: allow_request()?
    CB3-->>AM: true (CLOSED)
    AM->>AM: _busy_workers.add(w3)
    AM->>W3: POST /count {start:0, end:1.7GB}
    W3-->>AM: {result: {word:count,...}}
    AM->>CB3: record_success() → fail_count=0
    AM->>AM: _busy_workers.discard(w3) (finally)
    AM-->>CO: {chunk_id:0, status:ok, worker_id:w3, result:{...}}

    Note over CO,W3: Segundo chunk — CB1 y CB2 aún CLOSED (fail_count=1), nuevo dispatch → ya_fallaron = {}

    CO->>AM: POST /dispatch {chunk_id:1, inicio:1.7GB, fin:3.4GB, max_retries:3}

    Note over AM: Intento 1/3

    AM->>AM: _seleccionar_worker() → w1 (round-robin)
    AM->>CB1: allow_request()?
    CB1-->>AM: true (CLOSED, fail_count=1)
    AM->>AM: _busy_workers.add(w1)
    AM->>W1: POST /count {start:1.7GB, end:3.4GB}
    W1--xAM: Timeout
    AM->>CB1: record_failure() → fail_count=2 ≥ fail_max(2) → OPEN
    AM->>AM: _busy_workers.discard(w1) (finally)
    AM->>AM: ya_fallaron.add(w1)

    Note over AM: Intento 2/3 — ya_fallaron = {w1}

    AM->>AM: _seleccionar_worker(excluidos={w1}) → w2
    AM->>CB2: allow_request()?
    CB2-->>AM: true (CLOSED, fail_count=1)
    AM->>AM: _busy_workers.add(w2)
    AM->>W2: POST /count {start:1.7GB, end:3.4GB}
    W2-->>AM: {result:{...}}
    AM->>CB2: record_success() → fail_count=0
    AM->>AM: _busy_workers.discard(w2) (finally)
    AM-->>CO: {chunk_id:1, status:ok, result:{...}}

    Note over CB1: CB1 ahora OPEN — opened_at = now()
    Note over CB1: Tras 10s (reset_timeout) → allow_request() transiciona a HALF_OPEN
    Note over CB1: Próxima petición en HALF_OPEN será prueba de recuperación
```

---

## 4. Diagrama de Estados de Circuit Breaker

```mermaid
stateDiagram-v2
    [*] --> CLOSED

    CLOSED --> CLOSED : record_success()<br/>fail_count = 0
    CLOSED --> CLOSED : record_failure()<br/>fail_count < fail_max(2)
    CLOSED --> OPEN : record_failure()<br/>fail_count ≥ fail_max(2)<br/>opened_at = now()

    OPEN --> OPEN : record_failure()<br/>opened_at = now() (refresca timer)
    OPEN --> HALF_OPEN : allow_request()<br/>elapsed ≥ reset_timeout(10s)

    HALF_OPEN --> CLOSED : record_success()<br/>fail_count = 0, opened_at = null
    HALF_OPEN --> OPEN : record_failure()<br/>opened_at = now()

    note right of CLOSED
        Estado normal.
        allow_request() → true siempre.
        Peticiones pasan al worker.
    end note

    note right of OPEN
        Bloqueado por fallos consecutivos.
        allow_request() → false (fast-fail).
        Ambassador salta al siguiente worker
        en el Round-Robin.
    end note

    note left of HALF_OPEN
        Período de prueba.
        allow_request() → true.
        Una petición de prueba determina
        la transición:
        - record_success() → CLOSED
        - record_failure() → OPEN
    end note
```

---

## 5. Diagrama de Componentes

```mermaid
graph TB
    subgraph "Sistema WordCounter"

        subgraph "Coordinator (coordinator.py :4999)"
            COORD[Flask App]
            DASH["Dashboard Web<br/>(GET / → dashboard.html<br/>polling /status y /result c/3s)"]
            TP["ThreadPoolExecutor<br/>(despacho paralelo de chunks)"]
            PRL["Persistent Retry Loop<br/>(RETRY_DELAY=10s — reintenta<br/>chunks fallidos indefinidamente)"]
            CT["_total_counter: Counter<br/>(agregación de resultados)"]
            CHT["_chunk_times: dict<br/>(detección de chunks lentos >60s)"]
            HIST["_history: list<br/>(últimas 5 ejecuciones)"]
            SM["Máquina de estados<br/>idle → running → done|error"]
            GT["Ground Truth<br/>(conteo secuencial opcional)"]

            COORD --> DASH
            COORD --> TP
            COORD --> SM
            TP --> PRL
            PRL --> CT
            TP --> CHT
            CT --> HIST
            COORD --> GT
        end

        subgraph "Ambassador (ambassador.py :5005)"
            AM[Flask App<br/>threaded=True]
            RR["Round-Robin Inteligente<br/>(_rr_index + _rr_lock)<br/>salta busy + CB OPEN"]
            RETRY["Retry Logic<br/>MAX_RETRIES=3 (2 reintentos reales)<br/>ya_fallaron: set por dispatch"]
            BUSY["_busy_workers: set<br/>(anti-doble-asignación,<br/>marcado en finally)"]
            REGP["Persistencia<br/>_save/_load_registry()<br/>workers_registry.json"]
            HCK["GET /workers/health<br/>(ThreadPoolExecutor paralelo,<br/>timeout 3s por worker)"]
            AMBH["GET /health<br/>(healthcheck propio<br/>para Docker)"]

            AM --> RR
            AM --> RETRY
            RR --> BUSY
            AM --> REGP
            AM --> HCK
            AM --> AMBH
        end

        subgraph "Circuit Breakers (circuit_breaker.py)"
            CB1["CB: worker_1<br/>CLOSED|OPEN|HALF_OPEN<br/>fail_max=2, reset_timeout=10s<br/>thread-safe (RLock)"]
            CB2["CB: worker_2<br/>CLOSED|OPEN|HALF_OPEN"]
            CB3["CB: worker_N<br/>CLOSED|OPEN|HALF_OPEN"]
        end

        subgraph "Workers (worker.py :5001)"
            W1["Worker 1<br/>Host A"]
            W2["Worker 2<br/>Host B"]
            W3["Worker N<br/>Host C"]
            AR["Auto-registro<br/>_registration_loop(interval=15s)<br/>reintentos indefinidos"]
            MON["Monitoreo continuo<br/>GET /health al Ambassador c/15s<br/>si falla → re-registro automático"]
            WRD["Lectura por rango<br/>_ajustar_inicio() → alinea a frontera de palabra<br/>read bloques BUFFER_SIZE(64MB)<br/>BOUNDARY_BUF(512B) en límites de chunk"]
        end

        subgraph "Archivos locales (bind mount)"
            F1[("wiki_es.txt<br/>5.2 GB — Host A")]
            F2[("wiki_es.txt<br/>5.2 GB — Host B")]
            F3[("wiki_es.txt<br/>5.2 GB — Host C")]
            FC[("wiki_es.txt<br/>5.2 GB — Coordinator<br/>(chunk sizing + ground truth)")]
        end

        subgraph "Configuración (config.py)"
            CFG["AMBASSADOR_PORT=5005<br/>FAIL_MAX=2<br/>RESET_TIMEOUT=10s<br/>MAX_RETRIES=3<br/>REQUEST_TIMEOUT=600s<br/>BUFFER_SIZE=64MB<br/>BOUNDARY_BUF=512B"]
        end

    end

    COORD -->|"POST /dispatch {chunk_id, inicio, fin}"| AM
    COORD -->|"GET /workers/status"| AM
    COORD -->|"GET /workers/health"| AM
    COORD -->|"os.path.getsize() + lectura secuencial"| FC
    RR -->|"allow_request()"| CB1
    RR -->|"allow_request()"| CB2
    RR -->|"allow_request()"| CB3
    RETRY -->|"record_success/failure()"| CB1
    RETRY -->|"record_success/failure()"| CB2
    RETRY -->|"record_success/failure()"| CB3
    AM -->|"POST /count {start, end}"| W1
    AM -->|"POST /count {start, end}"| W2
    AM -->|"POST /count {start, end}"| W3
    AR -.->|"POST /register {url}"| AM
    MON -.->|"GET /health (c/15s)"| AM
    W1 -->|"seek + read bloques 64MB"| F1
    W2 -->|"seek + read bloques 64MB"| F2
    W3 -->|"seek + read bloques 64MB"| F3
    CFG -.->|"import config"| COORD
    CFG -.->|"import config"| AM
    CFG -.->|"import config"| W1
```

---

## 6. Diagrama de Secuencia — Reintentos persistentes automáticos (escenario de fallo)

```mermaid
sequenceDiagram
    participant U as Usuario
    participant CO as Coordinator :4999
    participant AM as Ambassador :5005
    participant W1 as Worker 1
    participant W2 as Worker 2

    Note over CO: Estado: running — 2 chunks (num_chunks = len(workers) = 2), W2 se cae

    par Chunk 0 → W1
        CO->>AM: POST /dispatch {chunk_id:0, inicio:0, fin:2.6GB}
        AM->>AM: _seleccionar_worker() → w1 (round-robin idx=0)
        AM->>AM: _busy_workers.add(w1)
        AM->>W1: POST /count {start:0, end:2.6GB}
        W1-->>AM: {worker_id:w1, result:{...}}
        AM->>AM: cb_w1.record_success() → fail_count=0
        AM->>AM: _busy_workers.discard(w1) (finally)
        AM-->>CO: {chunk_id:0, status:ok, result:{...}}
    and Chunk 1 → W2 (falla)
        CO->>AM: POST /dispatch {chunk_id:1, inicio:2.6GB, fin:5.2GB}
        AM->>AM: _seleccionar_worker() → w2 (round-robin idx=1)
        AM->>AM: _busy_workers.add(w2)
        AM->>W2: POST /count {start:2.6GB, end:5.2GB}
        W2--xAM: ConnectionError (worker caído)
        AM->>AM: cb_w2.record_failure() → fail_count=1
        AM->>AM: _busy_workers.discard(w2) (finally)
        AM->>AM: ya_fallaron.add(w2)
        Note over AM: Intento 2/3: _seleccionar_worker(excluidos={w2})
        Note over AM: w1 está en _busy_workers (chunk 0 aún procesa) → salta
        Note over AM: Ningún worker disponible → return None → 503 inmediato
        AM-->>CO: {chunk_id:1, status:error, reason:no_workers_available}
    end

    Note over CO: _run_processing: chunk_1 falló → pendientes = {chunk_1}
    Note over CO: Persistent Retry Loop — RETRY_DELAY = 10s

    CO->>CO: time.sleep(10) — espera antes de reintentar
    CO->>CO: intentos[1] = 2 (segundo intento a nivel Coordinator)

    Note over CO: Reintento persistente #1 — W1 ya está libre

    CO->>AM: POST /dispatch {chunk_id:1, inicio:2.6GB, fin:5.2GB}
    Note over AM: ya_fallaron = {} (set fresco por dispatch)
    AM->>AM: _seleccionar_worker() → w1 (CLOSED, fail_count=0)
    AM->>W1: POST /count {start:2.6GB, end:5.2GB}
    W1--xAM: Timeout (w1 sobrecargado)
    AM->>AM: cb_w1.record_failure() → fail_count=1
    AM->>AM: ya_fallaron.add(w1)
    Note over AM: Intento 2/3: _seleccionar_worker(excluidos={w1}) → w2
    AM->>AM: cb_w2: CLOSED, fail_count=1, allow_request()=true
    AM->>W2: POST /count {start:2.6GB, end:5.2GB}
    W2--xAM: ConnectionError (sigue caído)
    AM->>AM: cb_w2.record_failure() → fail_count=2 ≥ fail_max(2) → OPEN
    AM->>AM: ya_fallaron.add(w2)
    Note over AM: Intento 3/3: _seleccionar_worker(excluidos={w1,w2}) → None → 503
    AM-->>CO: {chunk_id:1, status:error, reason:no_workers_available}

    CO->>CO: time.sleep(10) — reintento persistente
    CO->>CO: intentos[1] = 3

    Note over CO: Reintento persistente #2

    CO->>AM: POST /dispatch {chunk_id:1, inicio:2.6GB, fin:5.2GB}
    AM->>AM: _seleccionar_worker() → w1 (CLOSED, fail_count=1)
    AM->>W1: POST /count {start:2.6GB, end:5.2GB}
    W1--xAM: Timeout
    AM->>AM: cb_w1.record_failure() → fail_count=2 ≥ fail_max(2) → OPEN
    AM->>AM: ya_fallaron.add(w1)
    Note over AM: Intento 2/3: w2 CB OPEN, allow_request()=false (elapsed<10s) → salta
    Note over AM: _seleccionar_worker(excluidos={w1}) → None → 503
    AM-->>CO: {chunk_id:1, status:error}

    CO->>CO: time.sleep(10) — el loop nunca se detiene
    CO->>CO: intentos[1] = 4

    Note over W2: W2 se recupera y su _registration_loop se re-registra

    W2->>AM: POST /register {url}
    AM->>AM: CB w2 estaba OPEN → crea nuevo CB en CLOSED (re-registro)
    AM->>AM: _save_registry()
    AM-->>W2: 200 OK {worker_id: w2}

    Note over CO: Reintento persistente #3
    Note over AM: CB w1: OPEN, 10s transcurridos → allow_request() transiciona a HALF_OPEN

    CO->>AM: POST /dispatch {chunk_id:1, inicio:2.6GB, fin:5.2GB}
    AM->>AM: _seleccionar_worker() → w1 (HALF_OPEN, allow_request()=true)
    AM->>W1: POST /count {start:2.6GB, end:5.2GB}
    W1--xAM: Timeout
    AM->>AM: cb_w1.record_failure() → HALF_OPEN → OPEN
    AM->>AM: ya_fallaron.add(w1)
    Note over AM: Intento 2/3: _seleccionar_worker(excluidos={w1}) → w2 (CLOSED, re-registrado)
    AM->>W2: POST /count {start:2.6GB, end:5.2GB}
    W2-->>AM: {worker_id:w2, result:{...}}
    AM->>AM: cb_w2.record_success() → fail_count=0
    AM-->>CO: {chunk_id:1, status:ok, worker_id:w2, result:{...}}

    Note over CO: Todos los chunks completados — pendientes = {}

    CO->>CO: total_counter.update() con resultados de chunks 0 y 1
    CO->>CO: reintentos_totales = sum(intentos[i] - 1) = 3 (chunk_0: 0, chunk_1: 3)
    CO->>CO: estado → "done" (siempre llega a 100%)
    CO->>CO: Guarda en _history (máx 5 entradas)

    Note over CO: Ground truth (si habilitado)
    CO->>CO: Conteo secuencial → speedup = t_seq / t_dist

    U->>CO: GET /result
    CO-->>U: {estado: done, exitosos: 2, num_chunks: 2, reintentos_totales: 3, ...}
```
