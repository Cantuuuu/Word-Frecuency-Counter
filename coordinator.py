"""
coordinator.py — Servidor Flask que espera la orden manual de inicio.

Endpoints:
  GET  /status  → workers registrados, estado actual y chunks lentos en curso
  GET  /result  → resultado del último procesamiento e historial de ejecuciones
  POST /start   → arranca el procesamiento (acepta {"retries": N, "ground_truth": bool})
  POST /reset   → vuelve a idle desde done/error (preserva historial)

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from flask import Flask, jsonify, request

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

_state  = "idle"   # idle | running | done | error
_result: dict = {}
_lock   = threading.Lock()

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

    Si max_retries se especifica, se incluye en el payload para que el
    Ambassador lo use en lugar de su config.MAX_RETRIES por defecto.
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
    global _state, _result

    try:
        chunks     = _calcular_chunks(FILE_PATH, num_chunks)
        t_inicio   = time.monotonic()
        resultados = {}

        with ThreadPoolExecutor(max_workers=num_chunks) as pool:
            futuros = {
                pool.submit(_enviar_chunk, i, start, end, max_retries): i
                for i, (start, end) in enumerate(chunks)
            }
            for futuro in as_completed(futuros):
                resp = futuro.result()
                cid  = resp.get("chunk_id", futuros[futuro])
                resultados[cid] = resp

        t_dist = time.monotonic() - t_inicio

        total_counter: Counter = Counter()
        exitosos = 0
        for cid, resp in resultados.items():
            if resp.get("status") == "ok":
                total_counter.update(resp.get("result", {}))
                exitosos += 1
            else:
                logger.warning(f"chunk_{cid} fallido: {resp.get('reason', 'desconocido')}")

        logger.info("=" * 60)
        logger.info(f"Chunks exitosos    : {exitosos} / {num_chunks}")
        logger.info(f"Tiempo distribuido : {t_dist:.2f}s")
        logger.info(f"Palabras únicas    : {len(total_counter):,}")
        logger.info("Top 20 palabras:")
        for palabra, cnt in total_counter.most_common(20):
            logger.info(f"  {palabra:<20} {cnt:>10,}")

        # Ground truth secuencial — opcional, costosa en archivos grandes
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
            "exitosos":           exitosos,
            "num_chunks":         num_chunks,
            "tiempo_distribuido": round(t_dist, 2),
            "tiempo_secuencial":  round(t_seq, 2) if t_seq is not None else None,
            "speedup":            round(t_seq / t_dist, 2) if t_seq and t_dist > 0 else None,
            "palabras_unicas":    len(total_counter),
            "top20":              total_counter.most_common(20),
        }

        with _lock:
            _state  = "done"
            _result = nuevo_resultado
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
    except Exception:
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
    """
    Resultado del último procesamiento e historial de las últimas ejecuciones.

    Útil para integración con otros sistemas o comparar corridas sin
    necesidad de revisar los logs manualmente.
    """
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

    Solo opera desde 'done' o 'error' — no interrumpe un procesamiento activo.
    Limpia el resultado actual pero preserva el historial de ejecuciones.

    Flujo de re-run recomendado:
        POST /reset  →  POST /start
    """
    global _state, _result

    with _lock:
        if _state == "running":
            return jsonify({"status": "error", "reason": "no se puede resetear mientras hay procesamiento activo"}), 409
        estado_anterior = _state
        _state  = "idle"
        _result = {}

    logger.info(f"Estado reseteado: {estado_anterior} → idle")
    return jsonify({"status": "ok", "estado_anterior": estado_anterior})


@app.route("/start", methods=["POST"])
def start():
    """
    Arranca el procesamiento con los workers registrados en este momento.

    Body opcional (JSON):
        retries      (int):  Reintentos máximos por chunk en el Ambassador.
                             Si se omite, el Ambassador usa su config.MAX_RETRIES.
        ground_truth (bool): Si false, omite el conteo secuencial al final.
                             Default: true. Útil para re-runs rápidos en demos.
    """
    global _state

    body         = request.get_json(silent=True) or {}
    max_retries  = body.get("retries")                        # None → Ambassador decide
    ground_truth = body.get("ground_truth", True)             # True → corre secuencial

    with _lock:
        if _state == "running":
            return jsonify({"status": "error", "reason": "ya está corriendo"}), 409
        _state = "running"

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

    num_chunks = len(workers)
    logger.info(f"Orden de inicio recibida — {num_chunks} worker(s): {workers}")
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
        "status":       "started",
        "workers":      workers,
        "num_chunks":   num_chunks,
        "max_retries":  max_retries,
        "ground_truth": ground_truth,
    })


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(f"Coordinator listo en puerto {PORT}")
    logger.info(f"  GET  http://localhost:{PORT}/status              → ver workers y estado")
    logger.info(f"  GET  http://localhost:{PORT}/result              → ver resultado e historial")
    logger.info(f'  POST http://localhost:{PORT}/start               → arrancar procesamiento')
    logger.info(f'       body opcional: {{"retries": N, "ground_truth": false}}')
    logger.info(f"  POST http://localhost:{PORT}/reset               → volver a idle (re-run)")
    app.run(host="0.0.0.0", port=PORT)
