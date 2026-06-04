"""
coordinator.py — Servidor Flask que espera la orden manual de inicio.

Endpoints:
  GET  /status       → workers, estado actual y chunks lentos en curso
  GET  /result       → resultado del último procesamiento e historial
  POST /start        → arranca el procesamiento
                       (acepta {"retries": N, "ground_truth": bool, "corpus_gb": X})
  POST /reset        → vuelve a idle desde done/partial/error (preserva historial)
  POST /reset-cbs    → resetea los Circuit Breakers (proxy al Ambassador)
  GET  /export.csv   → descarga el historial de ejecuciones en formato CSV
  POST /retry-failed → re-despacha solo los chunks que fallaron en la última corrida

Estados:
  idle    → sin procesar
  running → procesamiento en curso
  done    → todos los chunks completados (siempre llega a 100%)
  error   → fallo catastrófico (excepción no esperada)

Variables de entorno:
  FILE_PATH      = "/app/wiki_es.txt"
  AMBASSADOR_URL = "http://localhost:5005"
"""

import csv
import io
import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime

import requests
from flask import Flask, jsonify, render_template, request, Response

import config

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

FILE_PATH      = os.getenv("FILE_PATH",      config.WIKI_PATH)
AMBASSADOR_URL = os.getenv("AMBASSADOR_URL", "http://localhost:5005")
PORT           = int(os.getenv("PORT", "4999"))

SLOW_THRESHOLD = 60   # Segundos antes de marcar un chunk como "lento" en /status
MAX_HISTORY    = 50   # Número de ejecuciones guardadas en el historial (experimentos + fallos)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[COORD %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Estado global del procesamiento
# ---------------------------------------------------------------------------

_state  = "idle"   # idle | running | done | partial | error
_result: dict = {}
_lock   = threading.Lock()

# Counter completo preservado entre corridas para poder fusionar en /retry-failed
_total_counter: Counter = Counter()

# Tiempos de inicio por chunk — para detectar chunks lentos en /status
_chunk_times: dict[int, float] = {}
_chunk_times_lock = threading.Lock()

# Historial de las últimas MAX_HISTORY ejecuciones (protegido por _lock)
_history: list[dict] = []


# ---------------------------------------------------------------------------
# Consultar workers registrados en el Ambassador
# ---------------------------------------------------------------------------

def _get_workers() -> list[str]:
    r = requests.get(f"{AMBASSADOR_URL}/workers/status", timeout=5)
    r.raise_for_status()
    return r.json().get("registered", [])


def _get_workers_listos() -> list[str]:
    """
    Llama a GET /workers/health del Ambassador y retorna solo los workers
    que responden y tienen el archivo accesible.

    Si el endpoint no está disponible, retorna lista vacía para que el
    caller pueda degradar graciosamente.
    """
    r = requests.get(f"{AMBASSADOR_URL}/workers/health", timeout=10)
    r.raise_for_status()
    return r.json().get("listos", [])


# ---------------------------------------------------------------------------
# Dividir archivo en chunks de byte-range iguales
# ---------------------------------------------------------------------------

def _calcular_chunks(file_path: str, n: int, max_bytes: int | None = None) -> list[tuple[int, int]]:
    """
    Divide [0, total) en n rangos de bytes contiguos de tamaño parejo.

    Si se pasa max_bytes, el corpus lógico se limita a min(tamaño_real, max_bytes),
    de modo que un único archivo grande sirve para experimentar con distintos
    tamaños (1, 2, 3, 4, 5 GB) sin generar archivos separados. Los workers solo
    leen los rangos indicados; el resto del archivo se ignora.
    """
    total = os.path.getsize(file_path)
    if max_bytes is not None:
        total = min(total, max_bytes)

    size   = total // n
    chunks = []
    for i in range(n):
        start = i * size
        end   = start + size if i < n - 1 else total
        chunks.append((start, end))
    logger.info(f"Corpus lógico: {total:,} bytes → {n} chunks de ~{size:,} bytes c/u")
    return chunks


def _fin_palabra_parcial(bloque: bytes) -> int:
    """Índice tras el último byte de espacio de 'bloque' (o len si no hay ninguno).

    Idéntico a worker._fin_palabra_parcial: opera en bytes y corta en espacios
    para no partir palabras ni caracteres UTF-8 multibyte al decodificar.
    """
    i = len(bloque)
    while i > 0 and bloque[i - 1] not in b" \t\n\r":
        i -= 1
    return i


def _contar_secuencial(file_path: str, end_byte: int) -> Counter:
    """
    Conteo secuencial (ground truth) del rango [0, end_byte).

    Replica EXACTAMENTE la lógica de worker.count_words_from_file con start=0:
    lectura BINARIA en bloques de BUFFER_SIZE, carry-over del fragmento final de
    cada bloque y extensión del último bloque hasta el siguiente espacio. Al usar
    la misma tokenización que los workers, el resultado secuencial es idéntico a la
    unión de los chunks distribuidos cuando no se pierde ningún fragmento — esa
    es justamente la condición de correctitud que verificamos.

    NOTA: mantener idéntica a worker.count_words_from_file. Si cambia una, cambia
    la otra.
    """
    counter = Counter()
    with open(file_path, "rb") as file:
        remaining = end_byte
        carry     = b""
        while remaining > 0:
            to_read = min(config.BUFFER_SIZE, remaining)
            block   = file.read(to_read)
            if not block:
                break
            remaining -= len(block)
            block = carry + block

            if remaining <= 0:
                extra = file.read(config.BOUNDARY_BUF)
                corte = len(extra)
                for i, ch in enumerate(extra):
                    if ch in b" \t\n\r":
                        corte = i
                        break
                block += extra[:corte]
                carry  = b""
            else:
                pos   = _fin_palabra_parcial(block)
                carry = block[pos:]
                block = block[:pos]

            counter.update(re.findall(r"\b\w+\b", block.decode("utf-8", errors="ignore").lower()))

        if carry:
            counter.update(re.findall(r"\b\w+\b", carry.decode("utf-8", errors="ignore").lower()))

    return counter


# ---------------------------------------------------------------------------
# Enviar un chunk al Ambassador
# ---------------------------------------------------------------------------

def _enviar_chunk(
    chunk_id: int,
    inicio: int,
    fin: int,
    max_retries: int | None = None,
) -> dict:
    """
    Despacha un chunk al Ambassador y registra su tiempo de inicio para
    que /status pueda detectar chunks lentos.
    """
    logger.info(f"Enviando chunk_{chunk_id}: bytes {inicio:,} → {fin:,}")

    with _chunk_times_lock:
        _chunk_times[chunk_id] = time.monotonic()

    payload: dict = {"chunk_id": chunk_id, "inicio": inicio, "fin": fin}
    if max_retries is not None:
        payload["max_retries"] = max_retries

    try:
        r = requests.post(
            f"{AMBASSADOR_URL}/dispatch",
            json=payload,
            timeout=config.REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"chunk_{chunk_id}: excepción — {e}")
        return {"chunk_id": chunk_id, "worker_id": None, "status": "error", "reason": str(e)}
    finally:
        with _chunk_times_lock:
            _chunk_times.pop(chunk_id, None)


# ---------------------------------------------------------------------------
# Procesamiento distribuido (corre en hilo daemon)
# ---------------------------------------------------------------------------

def _run_processing(
    num_chunks: int,
    max_retries: int | None = None,
    ground_truth: bool = True,
    max_bytes: int | None = None,
    etiqueta: str = "",
) -> None:
    global _state, _result, _total_counter

    try:
        chunks       = _calcular_chunks(FILE_PATH, num_chunks, max_bytes)
        total_logico = chunks[-1][1]   # Bytes realmente procesados (respeta max_bytes)
        t_inicio     = time.monotonic()
        resultados: dict[int, dict] = {}

        RETRY_DELAY = 10  # Segundos de espera antes de reintentar un chunk fallido
        intentos: dict[int, int] = {}

        # --- Despacho con reintento persistente ---
        # Reintenta chunks fallidos indefinidamente hasta que todos
        # completen. Pausa RETRY_DELAY segundos entre reintentos para
        # dar tiempo a que los workers se reconecten.
        with ThreadPoolExecutor(max_workers=num_chunks) as pool:
            pendientes: set    = set()
            futuro_a_chunk: dict = {}

            for i, (start, end) in enumerate(chunks):
                f = pool.submit(_enviar_chunk, i, start, end, max_retries)
                pendientes.add(f)
                futuro_a_chunk[f] = i
                intentos[i] = 1

            while pendientes:
                completados, pendientes = wait(pendientes, return_when=FIRST_COMPLETED)

                for futuro in completados:
                    cid  = futuro_a_chunk.pop(futuro)
                    resp = futuro.result()

                    if resp.get("status") == "ok":
                        resultados[cid] = resp
                    else:
                        intentos[cid] += 1
                        logger.warning(
                            f"chunk_{cid} falló (intento {intentos[cid] - 1}) "
                            f"— reintentando en {RETRY_DELAY}s"
                        )
                        time.sleep(RETRY_DELAY)
                        start, end = chunks[cid]
                        new_f = pool.submit(_enviar_chunk, cid, start, end, max_retries)
                        pendientes.add(new_f)
                        futuro_a_chunk[new_f] = cid

        t_dist = time.monotonic() - t_inicio

        # --- Contar resultados finales (todos exitosos por diseño) ---
        total_counter: Counter = Counter()

        for cid, resp in resultados.items():
            total_counter.update(resp.get("result", {}))

        total_palabras_dist = sum(total_counter.values())

        estado_final = "done"
        reintentos_totales = sum(v - 1 for v in intentos.values())

        logger.info("=" * 60)
        logger.info(f"Chunks exitosos    : {num_chunks} / {num_chunks}  [DONE]")
        if reintentos_totales > 0:
            logger.info(f"Reintentos totales : {reintentos_totales}")
        logger.info(f"Tiempo distribuido : {t_dist:.2f}s")
        logger.info(f"Palabras únicas    : {len(total_counter):,}")
        logger.info("Top 20 palabras:")
        for palabra, cnt in total_counter.most_common(20):
            logger.info(f"  {palabra:<20} {cnt:>10,}")

        # --- Ground truth secuencial (opcional) ---
        t_seq               = None
        correcto            = None   # None = no se verificó (ground_truth=false)
        total_palabras_seq  = None
        palabras_unicas_seq = None
        if ground_truth:
            logger.info("Iniciando conteo secuencial (ground truth) ...")
            t0          = time.monotonic()
            seq_counter = _contar_secuencial(FILE_PATH, total_logico)
            t_seq       = time.monotonic() - t0

            total_palabras_seq  = sum(seq_counter.values())
            palabras_unicas_seq = len(seq_counter)

            # --- Verificación de correctitud contra el ground truth ---
            # El sistema distribuido es correcto si y solo si su Counter es
            # idéntico al secuencial: mismas palabras únicas Y misma frecuencia
            # en cada una. La comparación de Counter cubre ambas condiciones.
            correcto = (total_counter == seq_counter)

            logger.info("=" * 60)
            if correcto:
                logger.info("Correctitud        : ✓ IDÉNTICO al ground truth")
            else:
                logger.warning("Correctitud        : ✗ DIFIERE del ground truth")
                logger.warning(
                    f"  Palabras únicas dist/seq : "
                    f"{len(total_counter):,} / {palabras_unicas_seq:,}"
                )
                logger.warning(
                    f"  Total palabras  dist/seq : "
                    f"{total_palabras_dist:,} / {total_palabras_seq:,}"
                )
                # Mostrar hasta 10 palabras con conteo distinto para diagnosticar
                difs = []
                for palabra in set(total_counter) | set(seq_counter):
                    d, s = total_counter[palabra], seq_counter[palabra]
                    if d != s:
                        difs.append((palabra, d, s))
                        if len(difs) >= 10:
                            break
                for palabra, d, s in difs:
                    logger.warning(f"    {palabra:<20} dist={d:>12,}  seq={s:>12,}")

            logger.info("=" * 60)
            logger.info(f"Tiempo secuencial  : {t_seq:.2f}s")
            if t_dist < t_seq:
                logger.info(f"Speedup: {t_seq / t_dist:.2f}x más rápido")
            else:
                logger.info(f"Speedup: {t_dist / t_seq:.2f}x más lento")
        else:
            logger.info("Ground truth omitido (ground_truth=false) — correctitud no verificada")

        nuevo_resultado = {
            "etiqueta":                    etiqueta,
            "exitosos":                    num_chunks,
            "num_chunks":                  num_chunks,
            "reintentos_totales":          reintentos_totales,
            "tiempo_distribuido":          round(t_dist, 2),
            "tiempo_secuencial":           round(t_seq, 2) if t_seq is not None else None,
            "speedup":                     round(t_seq / t_dist, 2) if t_seq and t_dist > 0 else None,
            "correcto":                    correcto,
            "palabras_unicas":             len(total_counter),
            "palabras_unicas_secuencial":  palabras_unicas_seq,
            "total_palabras_distribuido":  total_palabras_dist,
            "total_palabras_secuencial":   total_palabras_seq,
            "archivo_bytes":               total_logico,
            "top20":                       total_counter.most_common(20),
        }

        with _lock:
            _state         = estado_final
            _result        = nuevo_resultado
            _total_counter = total_counter
            _history.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                **nuevo_resultado,
            })
            if len(_history) > MAX_HISTORY:
                _history.pop(0)

    except Exception as e:
        logger.error(f"Error durante procesamiento: {e}")
        with _lock:
            _state  = "error"
            _result = {"reason": str(e)}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/status", methods=["GET"])
def status():
    """
    Estado del sistema: workers, estado del procesamiento y chunks lentos.

    Un chunk se marca como 'lento' si lleva más de SLOW_THRESHOLD segundos
    sin responder — útil para detectar workers colgados sin revisar logs.
    """
    try:
        r       = requests.get(f"{AMBASSADOR_URL}/workers/status", timeout=5)
        workers = r.json() if r.status_code == 200 else {}
    except Exception as e:
        logger.warning(f"Ambassador no disponible en /status: {e}")
        workers = {}

    with _lock:
        estado = _state
        result = dict(_result)

    with _chunk_times_lock:
        now = time.monotonic()
        chunks_lentos = [
            {"chunk_id": cid, "segundos": round(now - t, 1)}
            for cid, t in _chunk_times.items()
            if now - t > SLOW_THRESHOLD
        ]
        chunks_en_proceso = len(_chunk_times)

    return jsonify({
        "estado":            estado,
        "chunks_en_proceso": chunks_en_proceso,
        "chunks_lentos":     chunks_lentos,
        "workers":           workers,
        "result":            result,
    })


@app.route("/result", methods=["GET"])
def result():
    """Resultado del último procesamiento e historial de las últimas ejecuciones."""
    with _lock:
        return jsonify({
            "estado":    _state,
            "result":    dict(_result),
            "historial": list(_history),
        })


@app.route("/reset", methods=["POST"])
def reset():
    """
    Vuelve el estado del coordinator a 'idle'.

    Opera desde done, partial o error — no interrumpe un procesamiento activo.
    Limpia el resultado actual y el counter acumulado, preserva el historial.

    Flujo de re-run recomendado:
        POST /reset  →  POST /start
    """
    global _state, _result, _total_counter

    with _lock:
        if _state == "running":
            return jsonify({"status": "error", "reason": "no se puede resetear mientras hay procesamiento activo"}), 409
        estado_anterior = _state
        _state         = "idle"
        _result        = {}
        _total_counter = Counter()

    logger.info(f"Estado reseteado: {estado_anterior} → idle")
    return jsonify({"status": "ok", "estado_anterior": estado_anterior})


@app.route("/reset-cbs", methods=["POST"])
def reset_cbs():
    """
    Resetea todos los Circuit Breakers a CLOSED (proxy al Ambassador).

    Permite limpiar el estado de los CB desde el dashboard, sin llamar al
    Ambassador directamente (que está en otro puerto/origen). Útil entre
    corridas de tolerancia a fallos para reincorporar workers bloqueados.
    """
    try:
        r = requests.post(f"{AMBASSADOR_URL}/workers/reset-cbs", timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        logger.warning(f"No se pudo resetear CBs en el Ambassador: {e}")
        return jsonify({"status": "error", "reason": str(e)}), 502


# Columnas del CSV exportable — mismas que experimentos/run_experiment.py
_CSV_COLUMNAS = [
    "timestamp", "etiqueta", "corpus_gb", "archivo_bytes", "n_workers",
    "t_secuencial_s", "t_distribuido_s", "speedup", "palabras_unicas",
    "total_palabras_dist", "total_palabras_seq", "correcto", "reintentos",
]


@app.route("/export.csv", methods=["GET"])
def export_csv():
    """Descarga el historial completo de ejecuciones en formato CSV."""
    with _lock:
        historial = list(_history)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNAS)
    writer.writeheader()
    for h in historial:
        archivo_bytes = h.get("archivo_bytes")
        writer.writerow({
            "timestamp":           h.get("timestamp"),
            "etiqueta":            h.get("etiqueta", ""),
            "corpus_gb":           round(archivo_bytes / 1024 ** 3, 4) if archivo_bytes else None,
            "archivo_bytes":       archivo_bytes,
            "n_workers":           h.get("num_chunks"),
            "t_secuencial_s":      h.get("tiempo_secuencial"),
            "t_distribuido_s":     h.get("tiempo_distribuido"),
            "speedup":             h.get("speedup"),
            "palabras_unicas":     h.get("palabras_unicas"),
            "total_palabras_dist": h.get("total_palabras_distribuido"),
            "total_palabras_seq":  h.get("total_palabras_secuencial"),
            "correcto":            h.get("correcto"),
            "reintentos":          h.get("reintentos_totales"),
        })

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=experimentos.csv"},
    )


@app.route("/retry-failed", methods=["POST"])
def retry_failed():
    """Obsoleto — el sistema ahora reintenta indefinidamente hasta completar."""
    return jsonify({
        "status": "info",
        "reason": "ya no es necesario: el sistema reintenta chunks fallidos hasta completar al 100%",
    })


@app.route("/start", methods=["POST"])
def start():
    """
    Arranca el procesamiento con los workers registrados en este momento.

    Antes de despachar, verifica la salud de cada worker (GET /workers/health
    en el Ambassador). Los workers sin archivo accesible o sin respuesta se
    excluyen automáticamente. Si el health check falla por completo, se usa
    la lista completa de workers registrados como fallback.

    Body opcional (JSON):
        retries      (int):   Reintentos máximos por chunk en el Ambassador.
        ground_truth (bool):  Si false, omite el conteo secuencial al final.
        corpus_gb    (float): Limita el corpus a procesar a estos GB (1, 2, 3...).
                              Útil para experimentar con distintos tamaños usando
                              un único archivo. Tiene prioridad sobre max_bytes.
        max_bytes    (int):   Límite del corpus en bytes (alternativa a corpus_gb).
                              Si se omiten ambos, se procesa el archivo completo.
    """
    global _state

    body         = request.get_json(silent=True) or {}
    max_retries  = body.get("retries")
    ground_truth = body.get("ground_truth", True)
    etiqueta     = (body.get("etiqueta") or "").strip()

    # Tamaño de corpus a procesar (corpus_gb tiene prioridad sobre max_bytes)
    corpus_gb = body.get("corpus_gb")
    max_bytes = body.get("max_bytes")
    if corpus_gb is not None:
        max_bytes = int(float(corpus_gb) * 1024 ** 3)
    if max_bytes is not None:
        max_bytes = int(max_bytes)
        if max_bytes <= 0:
            return jsonify({"status": "error", "reason": "corpus_gb/max_bytes debe ser > 0"}), 400

    with _lock:
        if _state == "running":
            return jsonify({"status": "error", "reason": "ya está corriendo"}), 409
        _state = "running"

    # Validar archivo antes de lanzar el hilo — error inmediato, no asíncrono
    if not os.path.isfile(FILE_PATH):
        with _lock:
            _state = "idle"
        return jsonify({"status": "error", "reason": f"archivo no encontrado: {FILE_PATH}"}), 500
    if os.path.getsize(FILE_PATH) == 0:
        with _lock:
            _state = "idle"
        return jsonify({"status": "error", "reason": f"archivo vacío: {FILE_PATH}"}), 500

    # Obtener workers registrados
    try:
        workers = _get_workers()
    except Exception as e:
        with _lock:
            _state = "idle"
        return jsonify({"status": "error", "reason": f"no se pudo consultar workers: {e}"}), 503

    if not workers:
        with _lock:
            _state = "idle"
        return jsonify({"status": "error", "reason": "no hay workers registrados"}), 503

    # Health check — filtrar workers sin archivo accesible o sin respuesta (C1)
    workers_excluidos = []
    try:
        listos = _get_workers_listos()
        workers_excluidos = [w for w in workers if w not in listos]
        if workers_excluidos:
            logger.warning(f"Workers excluidos (sin respuesta o archivo inaccesible): {workers_excluidos}")
        if listos:
            workers = listos
        else:
            logger.warning("Health check devolvió 0 workers listos — usando todos los registrados")
    except Exception as e:
        logger.warning(f"Health check falló: {e} — usando todos los workers registrados")

    if not workers:
        with _lock:
            _state = "idle"
        return jsonify({"status": "error", "reason": "no hay workers listos para procesar"}), 503

    num_chunks = len(workers)
    logger.info(f"Orden de inicio recibida — {num_chunks} worker(s) listos: {workers}")
    if workers_excluidos:
        logger.info(f"Workers excluidos: {workers_excluidos}")
    if max_retries is not None:
        logger.info(f"Reintentos configurados: {max_retries}")
    if not ground_truth:
        logger.info("Ground truth desactivado para esta corrida")
    if max_bytes is not None:
        if max_bytes < num_chunks:
            with _lock:
                _state = "idle"
            return jsonify({
                "status": "error",
                "reason": f"corpus demasiado pequeño ({max_bytes} bytes) para {num_chunks} workers",
            }), 400
        logger.info(f"Corpus limitado a: {max_bytes:,} bytes (~{max_bytes / 1024**3:.2f} GB)")

    threading.Thread(
        target=_run_processing,
        args=(num_chunks, max_retries, ground_truth, max_bytes, etiqueta),
        daemon=True,
    ).start()

    return jsonify({
        "status":             "started",
        "workers":            workers,
        "workers_excluidos":  workers_excluidos,
        "num_chunks":         num_chunks,
        "max_retries":        max_retries,
        "ground_truth":       ground_truth,
        "max_bytes":          max_bytes,
        "etiqueta":           etiqueta,
    })


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(f"Coordinator listo en puerto {PORT}")
    logger.info(f"  GET  http://localhost:{PORT}/status        → ver workers y estado")
    logger.info(f"  GET  http://localhost:{PORT}/result        → ver resultado e historial")
    logger.info(f'  POST http://localhost:{PORT}/start         → arrancar  [{{"retries": N, "ground_truth": false}}]')
    logger.info(f"  POST http://localhost:{PORT}/reset         → volver a idle")
    logger.info(f"  POST http://localhost:{PORT}/reset-cbs     → resetear Circuit Breakers (proxy Ambassador)")
    logger.info(f"  GET  http://localhost:{PORT}/export.csv    → descargar historial en CSV")
    logger.info(f"  POST http://localhost:{PORT}/retry-failed  → (obsoleto, reintentos ahora son automáticos)")
    app.run(host="0.0.0.0", port=PORT)
