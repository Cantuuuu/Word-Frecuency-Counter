"""
coordinator.py — Servidor Flask que espera la orden manual de inicio.

Endpoints:
  GET  /status       → workers, estado actual y chunks lentos en curso
  GET  /result       → resultado del último procesamiento e historial
  POST /start        → arranca el procesamiento (acepta {"retries": N, "ground_truth": bool})
  POST /reset        → vuelve a idle desde done/partial/error (preserva historial)
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

import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime

import requests
from flask import Flask, jsonify, render_template, request

import config

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

FILE_PATH      = os.getenv("FILE_PATH",      config.WIKI_PATH)
AMBASSADOR_URL = os.getenv("AMBASSADOR_URL", "http://localhost:5005")
PORT           = int(os.getenv("PORT", "4999"))

SLOW_THRESHOLD = 60   # Segundos antes de marcar un chunk como "lento" en /status
MAX_HISTORY    = 5    # Número de ejecuciones guardadas en el historial

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

def _calcular_chunks(file_path: str, n: int) -> list[tuple[int, int]]:
    total  = os.path.getsize(file_path)
    size   = total // n
    chunks = []
    for i in range(n):
        start = i * size
        end   = start + size if i < n - 1 else total
        chunks.append((start, end))
    logger.info(f"Archivo: {total:,} bytes → {n} chunks de ~{size:,} bytes c/u")
    return chunks


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
) -> None:
    global _state, _result, _total_counter

    try:
        chunks   = _calcular_chunks(FILE_PATH, num_chunks)
        t_inicio = time.monotonic()
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
        t_seq = None
        if ground_truth:
            logger.info("Iniciando conteo secuencial (ground truth) ...")
            t0          = time.monotonic()
            seq_counter: Counter = Counter()
            with open(FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    seq_counter.update(re.findall(r"\b\w+\b", line.lower()))
            t_seq = time.monotonic() - t0

            logger.info("=" * 60)
            logger.info(f"Tiempo secuencial  : {t_seq:.2f}s")
            if t_dist < t_seq:
                logger.info(f"Speedup: {t_seq / t_dist:.2f}x más rápido")
            else:
                logger.info(f"Speedup: {t_dist / t_seq:.2f}x más lento")
        else:
            logger.info("Ground truth omitido (ground_truth=false)")

        nuevo_resultado = {
            "exitosos":           num_chunks,
            "num_chunks":         num_chunks,
            "reintentos_totales": reintentos_totales,
            "tiempo_distribuido": round(t_dist, 2),
            "tiempo_secuencial":  round(t_seq, 2) if t_seq is not None else None,
            "speedup":            round(t_seq / t_dist, 2) if t_seq and t_dist > 0 else None,
            "palabras_unicas":    len(total_counter),
            "top20":              total_counter.most_common(20),
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
        retries      (int):  Reintentos máximos por chunk en el Ambassador.
        ground_truth (bool): Si false, omite el conteo secuencial al final.
    """
    global _state

    body         = request.get_json(silent=True) or {}
    max_retries  = body.get("retries")
    ground_truth = body.get("ground_truth", True)

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

    threading.Thread(
        target=_run_processing,
        args=(num_chunks, max_retries, ground_truth),
        daemon=True,
    ).start()

    return jsonify({
        "status":             "started",
        "workers":            workers,
        "workers_excluidos":  workers_excluidos,
        "num_chunks":         num_chunks,
        "max_retries":        max_retries,
        "ground_truth":       ground_truth,
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
    logger.info(f"  POST http://localhost:{PORT}/retry-failed  → (obsoleto, reintentos ahora son automáticos)")
    app.run(host="0.0.0.0", port=PORT)
