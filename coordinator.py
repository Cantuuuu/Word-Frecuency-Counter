"""
coordinator.py — Servidor Flask que espera la orden manual de inicio.

Endpoints:
  GET  /status   → muestra workers registrados y estado actual
  POST /start    → arranca el procesamiento con los workers que haya

Variables de entorno:
  FILE_PATH      = "/app/wiki_es.txt"
  AMBASSADOR_URL = "http://localhost:5005"
"""

import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

FILE_PATH      = os.getenv("FILE_PATH",      "/app/wiki_es.txt")
AMBASSADOR_URL = os.getenv("AMBASSADOR_URL", "http://localhost:5005")
PORT           = int(os.getenv("PORT", "4999"))

REQUEST_TIMEOUT = 600

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Estado global del procesamiento
# ---------------------------------------------------------------------------

_state  = "idle"   # idle | running | done | error
_result = {}
_lock   = threading.Lock()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[COORD {ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Consultar workers registrados
# ---------------------------------------------------------------------------

def _get_workers() -> list[str]:
    r = requests.get(f"{AMBASSADOR_URL}/workers/status", timeout=5)
    r.raise_for_status()
    return r.json().get("registered", [])


# ---------------------------------------------------------------------------
# Dividir archivo en chunks
# ---------------------------------------------------------------------------

def _calcular_chunks(file_path: str, n: int) -> list[tuple[int, int]]:
    total  = os.path.getsize(file_path)
    size   = total // n
    chunks = []
    for i in range(n):
        start = i * size
        end   = start + size if i < n - 1 else total
        chunks.append((start, end))
    _log(f"Archivo: {total:,} bytes → {n} chunks de ~{size:,} bytes c/u")
    return chunks


# ---------------------------------------------------------------------------
# Enviar un chunk al Ambassador
# ---------------------------------------------------------------------------

def _enviar_chunk(chunk_id: int, inicio: int, fin: int) -> dict:
    _log(f"Enviando chunk_{chunk_id}: bytes {inicio:,} → {fin:,}")
    try:
        r = requests.post(
            f"{AMBASSADOR_URL}/dispatch",
            json={"chunk_id": chunk_id, "inicio": inicio, "fin": fin},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _log(f"chunk_{chunk_id}: excepción — {e}")
        return {"chunk_id": chunk_id, "worker_id": None, "status": "error", "reason": str(e)}


# ---------------------------------------------------------------------------
# Procesamiento distribuido
# ---------------------------------------------------------------------------

def _run_processing(num_chunks: int) -> None:
    global _state, _result

    try:
        chunks     = _calcular_chunks(FILE_PATH, num_chunks)
        t_inicio   = time.monotonic()
        resultados = {}

        with ThreadPoolExecutor(max_workers=num_chunks) as pool:
            futuros = {
                pool.submit(_enviar_chunk, i, start, end): i
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
                _log(f"chunk_{cid} fallido: {resp.get('reason', 'desconocido')}")

        _log("=" * 60)
        _log(f"Chunks exitosos    : {exitosos} / {num_chunks}")
        _log(f"Tiempo distribuido : {t_dist:.2f}s")
        _log(f"Palabras únicas    : {len(total_counter):,}")
        _log("Top 20 palabras:")
        for palabra, cnt in total_counter.most_common(20):
            _log(f"  {palabra:<20} {cnt:>10,}")

        # Ground truth secuencial
        _log("Iniciando conteo secuencial (ground truth) ...")
        t0      = time.monotonic()
        seq_counter: Counter = Counter()
        with open(FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                seq_counter.update(re.findall(r"\b\w+\b", line.lower()))
        t_seq = time.monotonic() - t0

        _log("=" * 60)
        _log(f"Tiempo secuencial  : {t_seq:.2f}s")
        if t_dist < t_seq:
            _log(f"Speedup: {t_seq / t_dist:.2f}x más rápido")
        else:
            _log(f"Speedup: {t_dist / t_seq:.2f}x más lento")

        with _lock:
            _state  = "done"
            _result = {
                "exitosos":          exitosos,
                "num_chunks":        num_chunks,
                "tiempo_distribuido": round(t_dist, 2),
                "tiempo_secuencial":  round(t_seq, 2),
                "speedup":            round(t_seq / t_dist, 2) if t_dist > 0 else None,
                "palabras_unicas":   len(total_counter),
                "top20":             total_counter.most_common(20),
            }

    except Exception as e:
        _log(f"Error durante procesamiento: {e}")
        with _lock:
            _state  = "error"
            _result = {"reason": str(e)}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET"])
def status():
    """Muestra workers registrados y estado actual del procesamiento."""
    try:
        r        = requests.get(f"{AMBASSADOR_URL}/workers/status", timeout=5)
        workers  = r.json() if r.status_code == 200 else {}
    except Exception:
        workers  = {}

    with _lock:
        estado  = _state
        result  = dict(_result)

    return jsonify({
        "estado":   estado,
        "workers":  workers,
        "result":   result,
    })


@app.route("/start", methods=["POST"])
def start():
    """Arranca el procesamiento con los workers registrados en este momento."""
    global _state

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
    _log(f"Orden de inicio recibida — {num_chunks} worker(s): {workers}")

    threading.Thread(target=_run_processing, args=(num_chunks,), daemon=True).start()

    return jsonify({
        "status":     "started",
        "workers":    workers,
        "num_chunks": num_chunks,
    })


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _log(f"Coordinator listo en puerto {PORT}")
    _log(f"  GET  http://localhost:{PORT}/status  → ver workers y estado")
    _log(f"  POST http://localhost:{PORT}/start   → arrancar procesamiento")
    app.run(host="0.0.0.0", port=PORT)
