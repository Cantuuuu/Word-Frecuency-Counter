"""
coordinator.py — Divide el archivo wiki_es.txt en chunks y los despacha
al Ambassador en paralelo. El número de chunks se determina dinámicamente
según los workers registrados en el Ambassador.

Variables de entorno:
  FILE_PATH      = "/app/wiki_es.txt"
  AMBASSADOR_URL = "http://localhost:5005"
  MIN_WORKERS    = "1"   — mínimo de workers antes de arrancar
"""

import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

FILE_PATH      = os.getenv("FILE_PATH",      "/app/wiki_es.txt")
AMBASSADOR_URL = os.getenv("AMBASSADOR_URL", "http://localhost:5005")
MIN_WORKERS    = int(os.getenv("MIN_WORKERS", "1"))

REQUEST_TIMEOUT = 600
POLL_INTERVAL   = 2   # segundos entre consultas al Ambassador


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[COORD {ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# PASO 1 — Health check
# ---------------------------------------------------------------------------

def health_check() -> None:
    _log(f"Verificando Ambassador en {AMBASSADOR_URL}/health ...")
    try:
        r = requests.get(f"{AMBASSADOR_URL}/health", timeout=5)
        r.raise_for_status()
        _log("Ambassador OK ✓")
    except Exception as e:
        _log(f"ERROR: Ambassador no responde — {e}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# PASO 2 — Esperar workers registrados
# ---------------------------------------------------------------------------

def wait_for_workers(min_workers: int) -> int:
    """
    Consulta GET /workers/status hasta que haya al menos min_workers
    workers registrados. Retorna el número total de workers disponibles.
    """
    _log(f"Esperando al menos {min_workers} worker(s) registrado(s)...")
    while True:
        try:
            r = requests.get(f"{AMBASSADOR_URL}/workers/status", timeout=5)
            if r.status_code == 200:
                data = r.json()
                n    = data.get("n_registered", 0)
                _log(f"Workers registrados: {n} / {min_workers} mínimo — {data.get('registered', [])}")
                if n >= min_workers:
                    _log(f"Listo — procesando con {n} worker(s)")
                    return n
        except Exception as e:
            _log(f"No se pudo consultar workers: {e}")
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# PASO 3 — Dividir archivo en chunks
# ---------------------------------------------------------------------------

def calcular_chunks(file_path: str, n: int) -> list[tuple[int, int]]:
    total = os.path.getsize(file_path)
    size  = total // n
    chunks = []
    for i in range(n):
        start = i * size
        end   = start + size if i < n - 1 else total
        chunks.append((start, end))

    assert chunks[0][0] == 0
    assert chunks[-1][1] == total
    for i in range(len(chunks) - 1):
        assert chunks[i][1] == chunks[i + 1][0]

    _log(f"Archivo: {total:,} bytes → {n} chunks de ~{size:,} bytes c/u")
    return chunks


# ---------------------------------------------------------------------------
# PASO 4 — Enviar un chunk al Ambassador
# ---------------------------------------------------------------------------

def _enviar_chunk(chunk_id: int, inicio: int, fin: int) -> dict:
    payload = {"chunk_id": chunk_id, "inicio": inicio, "fin": fin}
    _log(f"Enviando chunk_{chunk_id}: bytes {inicio:,} → {fin:,}")
    try:
        r = requests.post(
            f"{AMBASSADOR_URL}/dispatch",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _log(f"chunk_{chunk_id}: excepción de red — {e}")
        return {"chunk_id": chunk_id, "worker_id": None, "status": "error", "reason": str(e)}


# ---------------------------------------------------------------------------
# PASO 5 — Distribuir y combinar resultados
# ---------------------------------------------------------------------------

def distribuido(file_path: str, num_chunks: int) -> tuple[Counter, float]:
    chunks = calcular_chunks(file_path, num_chunks)

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

    t_fin = time.monotonic()

    total_counter: Counter = Counter()
    exitosos = 0
    for cid, resp in resultados.items():
        if resp.get("status") == "ok":
            total_counter.update(resp.get("result", {}))
            exitosos += 1
        else:
            _log(f"chunk_{cid} fallido: {resp.get('reason', 'desconocido')}")

    elapsed = t_fin - t_inicio
    _log("=" * 60)
    _log(f"Chunks exitosos    : {exitosos} / {num_chunks}")
    _log(f"Tiempo distribuido : {elapsed:.2f}s")
    _log(f"Palabras únicas    : {len(total_counter):,}")
    _log("Top 20 palabras:")
    for palabra, cnt in total_counter.most_common(20):
        _log(f"  {palabra:<20} {cnt:>10,}")

    return total_counter, elapsed


# ---------------------------------------------------------------------------
# PASO 6 — Ground truth secuencial
# ---------------------------------------------------------------------------

def sequential_count(file_path: str) -> tuple[Counter, float]:
    _log("Iniciando conteo secuencial (ground truth) ...")
    t0      = time.monotonic()
    counter: Counter = Counter()
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            words = re.findall(r"\b\w+\b", line.lower())
            counter.update(words)
    elapsed = time.monotonic() - t0
    return counter, elapsed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    health_check()

    num_workers  = wait_for_workers(MIN_WORKERS)
    result_dist, t_dist = distribuido(FILE_PATH, num_workers)

    result_seq, t_seq = sequential_count(FILE_PATH)

    _log("=" * 60)
    _log(f"Tiempo secuencial  : {t_seq:.2f}s")

    if t_dist < t_seq:
        _log(f"Speedup: {t_seq / t_dist:.2f}x más rápido")
    else:
        _log(f"Speedup: {t_dist / t_seq:.2f}x más lento")
