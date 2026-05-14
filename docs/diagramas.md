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

    U -->|"POST /start<br>POST /reset<br>POST /retry-failed<br>GET /status<br>GET /result"| CO
    U -->|"GET /workers/status<br>GET /workers/health<br>POST /workers/reset-cbs<br>DELETE /workers/id"| AM
    CO -->|"POST /dispatch<br>GET /workers/status<br>GET /workers/health"| AM
    AM -->|"POST /count<br>GET /health"| WL
    AM -->|"POST /count<br>GET /health"| W1
    AM -->|"POST /count<br>GET /health"| W2
    WL -.->|"POST /register<br>(auto-registro al arrancar)"| AM
    W1 -.->|"POST /register<br>(auto-registro al arrancar)"| AM
    W2 -.->|"POST /register<br>(auto-registro al arrancar)"| AM

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

    Note over W1,W2: Fase 0 — Auto-registro al arrancar (hasta 15 intentos, c/2s)

    W1->>AM: POST /register {worker_id, url}
    AM->>AM: Crea CircuitBreaker(w1, fail_max=2, reset_timeout=10)
    AM->>AM: _save_registry() → workers_registry.json
    AM-->>W1: 200 OK {total_workers: 1}
    W2->>AM: POST /register {worker_id, url}
    AM->>AM: Crea CircuitBreaker(w2, fail_max=2, reset_timeout=10)
    AM->>AM: _save_registry() → workers_registry.json
    AM-->>W2: 200 OK {total_workers: 2}

    Note over U,W2: Fase 1 — Inicio del procesamiento

    U->>CO: POST /start {retries: 2, ground_truth: true}
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
        CO->>AM: POST /dispatch {chunk_id:0, inicio:0, fin:2.6GB, max_retries:2}
        AM->>AM: _seleccionar_worker() → Round-Robin + CB allow_request() + busy check
        AM->>W1: POST /count {start:0, end:2.6GB}
        W1->>W1: _ajustar_inicio(start) → alinea a frontera de palabra
        W1->>W1: read en bloques de 64MB + BOUNDARY_BUF(512B) en límites
        W1->>W1: re.findall(r"\b\w+\b", chunk.lower()) → Counter
        W1-->>AM: {worker_id:w1, result:{word:count,...}}
        AM->>AM: cb_w1.record_success() → fail_count=0
        AM-->>CO: {chunk_id:0, status:ok, result:{...}}
    and Chunk 1
        CO->>AM: POST /dispatch {chunk_id:1, inicio:2.6GB, fin:5.2GB, max_retries:2}
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
    CO->>CO: estado → "done" (2/2 exitosos)
    CO->>CO: _total_counter preservado para posible /retry-failed

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
    participant W1 as Worker 1
    participant W2 as Worker 2

    Note over CB1,CB2: Ambos CB inician en CLOSED (fail_count=0, fail_max=2)

    CO->>AM: POST /dispatch {chunk_id:0, inicio:0, fin:2.6GB, max_retries:2}

    Note over AM: Intento 1/2 — ya_fallaron = {}

    AM->>AM: _seleccionar_worker() → w1
    AM->>CB1: allow_request()?
    CB1-->>AM: true (CLOSED)
    AM->>AM: _busy_workers.add(w1)
    AM->>W1: POST /count {start:0, end:2.6GB} (timeout=600s)
    W1--xAM: Timeout / ConnectionError
    AM->>CB1: record_failure() → fail_count=1
    AM->>AM: _busy_workers.discard(w1) (finally)
    AM->>AM: ya_fallaron.add(w1)

    Note over AM: Intento 2/2 — ya_fallaron = {w1}

    AM->>AM: _seleccionar_worker(excluidos={w1}) → w2
    AM->>CB2: allow_request()?
    CB2-->>AM: true (CLOSED)
    AM->>AM: _busy_workers.add(w2)
    AM->>W2: POST /count {start:0, end:2.6GB}
    W2-->>AM: {result: {word:count,...}}
    AM->>CB2: record_success() → fail_count=0
    AM->>AM: _busy_workers.discard(w2) (finally)
    AM-->>CO: {chunk_id:0, status:ok, worker_id:w2, result:{...}}

    Note over CO,W2: Segundo chunk — CB1 aún CLOSED (fail_count=1), nuevo dispatch → ya_fallaron = {}

    CO->>AM: POST /dispatch {chunk_id:1, inicio:2.6GB, fin:5.2GB, max_retries:2}

    Note over AM: Intento 1/2

    AM->>AM: _seleccionar_worker() → w1 (round-robin)
    AM->>CB1: allow_request()?
    CB1-->>AM: true (CLOSED, fail_count=1)
    AM->>AM: _busy_workers.add(w1)
    AM->>W1: POST /count {start:2.6GB, end:5.2GB}
    W1--xAM: Timeout
    AM->>CB1: record_failure() → fail_count=2 ≥ fail_max(2) → OPEN
    AM->>AM: _busy_workers.discard(w1) (finally)
    AM->>AM: ya_fallaron.add(w1)

    Note over AM: Intento 2/2 — ya_fallaron = {w1}

    AM->>AM: _seleccionar_worker(excluidos={w1}) → w2
    AM->>CB2: allow_request()?
    CB2-->>AM: true (CLOSED)
    AM->>AM: _busy_workers.add(w2)
    AM->>W2: POST /count {start:2.6GB, end:5.2GB}
    W2-->>AM: {result:{...}}
    AM->>CB2: record_success()
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
            TP["ThreadPoolExecutor<br/>(despacho paralelo de chunks)"]
            CT["_total_counter: Counter<br/>(agregación, preservado para /retry-failed)"]
            CHT["_chunk_times: dict<br/>(detección de chunks lentos >60s)"]
            HIST["_history: list<br/>(últimas 5 ejecuciones)"]
            SM["Máquina de estados<br/>idle → running → done|partial|error"]
            GT["Ground Truth<br/>(conteo secuencial opcional)"]

            COORD --> TP
            COORD --> SM
            TP --> CT
            TP --> CHT
            CT --> HIST
            COORD --> GT
        end

        subgraph "Ambassador (ambassador.py :5005)"
            AM[Flask App<br/>threaded=True]
            RR["Round-Robin Inteligente<br/>(_rr_index + _rr_lock)<br/>salta busy + CB OPEN"]
            RETRY["Retry Logic<br/>MAX_RETRIES=2 (1 reintento real)<br/>ya_fallaron: set por dispatch"]
            BUSY["_busy_workers: set<br/>(anti-doble-asignación,<br/>marcado en finally)"]
            REGP["Persistencia<br/>_save/_load_registry()<br/>workers_registry.json"]
            HCK["GET /workers/health<br/>(ThreadPoolExecutor paralelo,<br/>timeout 3s por worker)"]

            AM --> RR
            AM --> RETRY
            RR --> BUSY
            AM --> REGP
            AM --> HCK
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
            AR["Auto-registro<br/>register_with_ambassador()<br/>hasta 15 intentos, c/2s"]
            WRD["Lectura por rango<br/>_ajustar_inicio() → alinea a frontera de palabra<br/>read bloques BUFFER_SIZE(64MB)<br/>BOUNDARY_BUF(512B) en límites de chunk"]
        end

        subgraph "Archivos locales (bind mount)"
            F1[("wiki_es.txt<br/>5.2 GB — Host A")]
            F2[("wiki_es.txt<br/>5.2 GB — Host B")]
            F3[("wiki_es.txt<br/>5.2 GB — Host C")]
            FC[("wiki_es.txt<br/>5.2 GB — Coordinator<br/>(chunk sizing + ground truth)")]
        end

        subgraph "Configuración (config.py)"
            CFG["AMBASSADOR_PORT=5005<br/>FAIL_MAX=2<br/>RESET_TIMEOUT=10s<br/>MAX_RETRIES=2<br/>REQUEST_TIMEOUT=600s<br/>BUFFER_SIZE=64MB<br/>BOUNDARY_BUF=512B"]
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
    AR -.->|"POST /register {worker_id, url}"| AM
    W1 -->|"seek + read bloques 64MB"| F1
    W2 -->|"seek + read bloques 64MB"| F2
    W3 -->|"seek + read bloques 64MB"| F3
    CFG -.->|"import config"| COORD
    CFG -.->|"import config"| AM
    CFG -.->|"import config"| W1
```

---

## 6. Diagrama de Secuencia — Fallo parcial y retry-failed

```mermaid
sequenceDiagram
    participant U as Usuario
    participant CO as Coordinator :4999
    participant AM as Ambassador :5005
    participant W1 as Worker 1
    participant W2 as Worker 2

    Note over CO: Estado: running — 3 chunks, W2 se cayó

    par Chunk 0 → W1
        CO->>AM: POST /dispatch {chunk_id:0, inicio:0, fin:X}
        AM->>W1: POST /count {start:0, end:X}
        W1-->>AM: {status:ok, result:{...}}
        AM->>AM: cb_w1.record_success()
        AM-->>CO: {chunk_id:0, status:ok}
    and Chunk 1 → W2
        CO->>AM: POST /dispatch {chunk_id:1, inicio:X, fin:Y}
        AM->>W2: POST /count {start:X, end:Y}
        W2--xAM: ConnectionError (worker caído)
        AM->>AM: cb_w2.record_failure()
        AM->>AM: retry: _seleccionar_worker(excluidos={w2}) → w1
        AM->>W1: POST /count {start:X, end:Y}
        W1--xAM: Timeout (ocupado con chunk 0)
        AM->>AM: cb_w1.record_failure()
        AM-->>CO: {chunk_id:1, status:error, reason:max_retries_exceeded}
    and Chunk 2 → W1
        CO->>AM: POST /dispatch {chunk_id:2, inicio:Y, fin:Z}
        AM->>W1: POST /count {start:Y, end:Z}
        W1-->>AM: {status:ok, result:{...}}
        AM->>AM: cb_w1.record_success()
        AM-->>CO: {chunk_id:2, status:ok}
    end

    Note over CO: Fase 2 — Auto-reintento automático de chunks fallidos
    CO->>CO: fallidos_ids = [1] → reintentando con ThreadPoolExecutor
    CO->>AM: POST /dispatch {chunk_id:1, inicio:X, fin:Y} (reintento automático)
    AM->>AM: _seleccionar_worker() → w1 (w2 CB puede estar OPEN)
    AM->>W1: POST /count {start:X, end:Y}
    W1--xAM: Timeout (saturado)
    AM-->>CO: {chunk_id:1, status:error}

    Note over CO: Fase 3 — Resultado parcial
    CO->>CO: exitosos = 2, num_chunks = 3 → estado = "partial"
    CO->>CO: total_counter.update() solo chunks 0 y 2
    CO->>CO: _total_counter preservado para fusión futura
    CO->>CO: chunks_fallidos = [{chunk_id:1, inicio:X, fin:Y, reason:...}]
    CO->>CO: Guarda en _history

    U->>CO: GET /result
    CO-->>U: {estado: partial, exitosos: 2, num_chunks: 3, chunks_fallidos: [...]}

    Note over U,W2: Más tarde — W2 se recupera y se re-registra

    W2->>AM: POST /register {worker_id:w2, url:...}
    AM->>AM: CB w2 estaba OPEN → crea nuevo CB en CLOSED (re-registro)
    AM->>AM: _save_registry()
    AM-->>W2: 200 OK

    U->>CO: POST /retry-failed
    CO->>CO: Valida estado == "partial", estado → running
    CO->>CO: Lee chunks_fallidos → [{chunk_id:1, inicio:X, fin:Y}]
    CO->>AM: POST /dispatch {chunk_id:1, inicio:X, fin:Y}
    AM->>AM: _seleccionar_worker() → w2 (CB CLOSED)
    AM->>W2: POST /count {start:X, end:Y}
    W2-->>AM: {status:ok, result:{...}}
    AM->>AM: cb_w2.record_success()
    AM-->>CO: {chunk_id:1, status:ok, result:{...}}

    CO->>CO: _total_counter.update(result chunk_1) → fusión con chunks 0+2
    CO->>CO: exitosos = 2+1 = 3 == num_chunks → estado = "done"
    CO-->>U: {status:ok, recuperados:1, aun_fallidos:0, estado:done}
```
