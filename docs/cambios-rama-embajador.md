# Cambios en la rama `embajador`

Documento para el equipo: qué se modificó respecto a las ramas `coordinator` y `sphnxBranch`, y por qué.

---

## Parte 1 — `embajador` vs `coordinator`

La rama `coordinator` tiene el sistema base con el Coordinator implementado. La rama `embajador` toma ese trabajo y agrega correcciones de bugs, documentación completa y el health poller.

---

### `ambassador.py` — 4 cambios

#### 1. Traducción de payload `inicio/fin → start/end`

**Problema:** El Coordinator envía `{inicio, fin}` al Ambassador, pero el Worker (actualizado en la rama de Sphnx) espera `{start, end}`. El Ambassador enviaba el payload sin traducir, por lo que el Worker recibía campos que no reconocía y procesaba el chunk desde el byte 0 siempre.

**Cambio:**
```python
# ANTES — el payload llegaba tal cual al worker:
respuesta = requests.post(url, json=payload, ...)

# DESPUÉS — el Ambassador traduce antes de reenviar:
worker_payload = {"start": payload["inicio"], "end": payload["fin"]}
respuesta = requests.post(url, json=worker_payload, ...)
```

**Por qué aquí:** El Ambassador es el punto natural de traducción. El Coordinator no sabe que el Worker habla inglés, y el Worker no sabe que el Coordinator habla español. El Ambassador absorbe esa diferencia sin tocar ninguno de los dos extremos.

---

#### 2. Bug: log de recuperación HALF_OPEN → CLOSED nunca disparaba

**Problema:** La condición original para loguear la recuperación de un worker era:
```python
if cb.state.value == "CLOSED" and worker_id in ya_fallaron:
```
Esto nunca era `True` porque `_seleccionar_worker` excluye los workers que están en `ya_fallaron`, por lo que el worker seleccionado nunca pertenece a ese conjunto. El mensaje `"Prueba exitosa: CB worker_N → CLOSED 🟢"` jamás aparecía en consola.

**Cambio:**
```python
# Capturar el estado ANTES del call
era_half_open = cb.state.value == "HALF_OPEN"
...
# Después del éxito, verificar con el flag capturado
if era_half_open:
    _log(f"Prueba exitosa: CB {worker_id} → CLOSED 🟢")
```

**Por qué:** El estado se captura antes de que `record_success()` lo modifique. Así el flag refleja correctamente si era una prueba de recuperación.

---

#### 3. Bug: `import time` dentro del loop de reintentos

**Problema:** El módulo `time` se importaba como `import time as _time` dentro del bloque `try` en cada iteración del loop. Python cachea los imports, así que no causaba lecturas repetidas de disco, pero es un anti-patrón: oculta la dependencia y ejecuta código innecesario en el hot path.

**Cambio:** `import time` movido al nivel de módulo (línea 38), junto con los demás imports.

---

#### 4. Health Poller — nuevo

**Adición:** Un hilo de fondo que monitorea proactivamente el estado de cada worker cada 10 segundos llamando a `GET /health`, sin esperar a que llegue un chunk.

```
Sin poller: el CB solo se actualiza cuando el Ambassador intenta enviar trabajo.
            Si un worker cae entre chunks, nadie lo sabe hasta el siguiente intento.

Con poller: el Ambassador sabe en tiempo real si un worker está caído,
            y el CB se actualiza aunque no haya chunks en vuelo.
```

El intervalo es igual a `RESET_TIMEOUT` (10s) para sincronizarse con el ciclo de recuperación del CB. Solo loguea cuando el estado cambia, no en cada check.

---

### `worker.py` — 1 cambio funcional

#### Campos renombrados de `inicio/fin` a `start/end`

El endpoint `/count` ahora lee `start` y `end` del JSON en lugar de `inicio`, `fin` y `chunk_id`. El Ambassador hace la traducción, así que el Worker recibe los nombres correctos.

```python
# ANTES
inicio   = datos.get("inicio")
fin      = datos.get("fin")
chunk_id = datos.get("chunk_id")

# DESPUÉS
start = datos.get("start")
end   = datos.get("end")
```

El resto del archivo (lectura binaria, regex, `errors="replace"`) no cambió — solo se mejoraron los comentarios explicando el por qué de cada decisión.

---

### `coordinator.py` — correcciones de calidad (sin cambio funcional)

El Coordinator de la rama `coordinator` funcionaba correctamente. Se corrigieron cuatro problemas de calidad detectados en revisión:

| # | Problema | Corrección |
|---|----------|------------|
| 1 | `assert` como guards de runtime | Reemplazados por `raise ValueError` — `assert` se desactiva con `python -O` |
| 2 | Tipo de retorno `-> Counter` incorrecto | Corregido a `-> tuple[Counter, float]` |
| 3 | Regex `r"\b\w+\b"` en `sequential_count` diferente al del Worker | Alineada a `r"\b[a-záéíóúüñ]+\b"` — sin esto el Speedup se comparaba con conteos incompatibles |
| 4 | `errors="ignore"` en `sequential_count` | Cambiado a `errors="replace"` para ser consistente con `worker.py` |

También se agregó `SystemExit(1)` si algún chunk falla, para que el proceso retorne código de error detectable.

---

### `docker-compose.yml` — 2 cambios

**1. Volumen del worker local corregido:**
```yaml
# ANTES (incorrecto — la ruta interna no coincidía con WIKI_PATH del worker)
- ./wiki_es.txt:/app/data/input.txt:ro

# DESPUÉS (correcto — coincide con el default de WIKI_PATH en worker.py)
- ./wiki_es.txt:/app/wiki_es.txt:ro
```

**2. Coordinator activado con configuración correcta:**
El servicio `coordinator` estaba comentado. Se activó y se configuró con:
- `restart: "no"` — es un proceso one-shot, no un servidor; no debe reiniciarse en bucle
- Sin `ports` — el Coordinator no expone ningún puerto HTTP
- `AMBASSADOR_URL`, `FILE_PATH`, `NUM_CHUNKS` como variables de entorno

---

### Archivos nuevos en `embajador`

| Archivo | Descripción |
|---------|-------------|
| `Dockerfile.coordinator` | Imagen Docker para el Coordinator (tomado de la rama coordinator) |
| `circuit_breaker.py` | CB del compañero Sphnx (con bug corregido — ver Parte 2) |
| `docs/cambios-rama-embajador.md` | Este documento |
| `docs/superpowers/plans/` | Plan de containerización para 4 laptops |

---

---

## Parte 2 — `embajador` vs `sphnxBranch`

La rama `sphnxBranch` aporta `circuit_breaker.py` y una versión mejorada de `worker.py`. La rama `embajador` integra ambos y corrige un bug crítico.

---

### `circuit_breaker.py` — bug crítico corregido

**Problema original en `sphnxBranch`:**

```python
def record_failure(self):
    self.failure_count += 1
    self.last_failure_time = time.time()

    if self.state == "HALF_OPEN":
        self.state = "OPEN"
    return                              # ← return aquí

    if self.failure_count >= self.failure_threshold:  # ← NUNCA SE EJECUTA
        self.state = "OPEN"
```

El `return` antes del segundo `if` hace que la transición `CLOSED → OPEN` sea código muerto. Consecuencia: el Circuit Breaker **nunca se abría** cuando un worker fallaba repetidamente desde estado CLOSED. El sistema seguía enviando trabajo a workers caídos indefinidamente.

**Corrección en `embajador`:**

```python
def record_failure(self):
    self.failure_count += 1
    self.last_failure_time = time.time()

    if self.state == "HALF_OPEN":
        self.state = "OPEN"
    elif self.failure_count >= self.failure_threshold:  # ← ahora sí se ejecuta
        self.state = "OPEN"
```

El `return` se elimina y el segundo bloque se convierte en `elif`, expresando correctamente que las dos transiciones son mutuamente excluyentes.

---

### Diferencias de interfaz: `circuit_breaker.py` vs `circuit_breaker_mock.py`

El Ambassador usa `circuit_breaker_mock.py` (nuestro). El `circuit_breaker.py` de Sphnx tiene una interfaz diferente. Esta tabla es importante si algún día se decide migrar:

| Aspecto | `circuit_breaker_mock.py` (en uso) | `circuit_breaker.py` (Sphnx) |
|---------|----------------------------------|-------------------------------|
| Constructor | `CircuitBreaker(name, fail_max, reset_timeout)` | `CircuitBreaker(failure_threshold, recovery_timeout)` — sin `name` |
| `state` | Enum `State.CLOSED / OPEN / HALF_OPEN` | String `"CLOSED" / "OPEN" / "HALF_OPEN"` |
| `allow_request()` | Retorna `bool` (`True`/`False`) | Retorna `True` o lanza `CircuitBreakerOpen` si OPEN |
| Estado en `/workers/status` | `cb.get_status()` → dict completo | `cb.get_state()` → solo el string de estado |
| Thread-safety | Sí (`threading.RLock` interno) | No — requiere lock externo |

**Si se integra `circuit_breaker.py` de Sphnx**, habría que cambiar en `ambassador.py`:
1. La creación del CB: quitar el parámetro `name`
2. El `_seleccionar_worker`: capturar `CircuitBreakerOpen` en lugar de verificar el bool
3. El endpoint `/workers/status`: llamar a `get_state()` en lugar de `get_status()`

---

### `worker.py` — diferencias con `sphnxBranch`

La rama `sphnxBranch` tiene su propia versión de `worker.py`. Las diferencias respecto a `embajador`:

| Aspecto | `sphnxBranch` | `embajador` |
|---------|--------------|-------------|
| Campos del request | `start`, `end` (sin `chunk_id`) | `start`, `end` — igual |
| Ruta del archivo | `FILE_PATH` (var de entorno) | `WIKI_PATH` (var de entorno) |
| Nombre variable env | `FILE_PATH` | `WIKI_PATH` |
| Ruta default | `/app/data/input.txt` | `/app/wiki_es.txt` |
| Manejo OOM | Lectura por bloques de 64 MB + alineación a fronteras de palabra | Lectura completa del chunk en memoria |
| `FAIL_MODE` / `DELAY` | Sí | Sí |
| Regex | `\b\w+\b` | `\b[a-záéíóúüñ]+\b` |

> **Nota importante sobre la ruta del archivo:** La rama `sphnxBranch` usa `FILE_PATH=/app/data/input.txt` como default. La rama `embajador` usa `WIKI_PATH=/app/wiki_es.txt`. El `docker-compose.yml` y los Dockerfiles de `embajador` montan el archivo en `/app/wiki_es.txt`, por lo que son consistentes entre sí.

> **Nota sobre el manejo de OOM:** La versión de Sphnx incluye una mejora importante para archivos muy grandes: lee el chunk en bloques de 64 MB y ajusta los límites del chunk a fronteras de palabra para no cortar palabras en el borde. Esto no está en `embajador` aún — es un candidato para integrar en una próxima iteración.

---

## Resumen ejecutivo para el equipo

| Cambio | Dónde | Impacto |
|--------|-------|---------|
| Traducción `inicio/fin → start/end` | `ambassador.py` | **Crítico** — sin esto los workers procesaban siempre desde byte 0 |
| Bug log HALF_OPEN nunca disparaba | `ambassador.py` | Medio — el CB funcionaba bien, solo el log estaba roto |
| `import time` dentro del loop | `ambassador.py` | Menor — anti-patrón, sin impacto real |
| Health poller proactivo | `ambassador.py` | Funcional — el CB ahora se actualiza aunque no haya chunks |
| Campos `start`/`end` en worker | `worker.py` | **Crítico** — necesario para recibir el payload del Ambassador |
| Bug `CLOSED→OPEN` en CB | `circuit_breaker.py` | **Crítico** — sin la corrección el CB nunca abría el circuito |
| `assert` → `raise ValueError` | `coordinator.py` | Calidad — los assert se deshabilitan con `-O` |
| Regex alineada con worker | `coordinator.py` | Corrección — el Speedup se calculaba con conteos incomparables |
| `docker-compose.yml` coordinator | infraestructura | Funcional — ahora el coordinator arranca correctamente |
