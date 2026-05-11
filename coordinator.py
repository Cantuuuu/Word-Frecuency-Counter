"""
coordinator.py — Divide el archivo wiki_es.txt en chunks y los despacha
al Ambassador en paralelo. Combina resultados y muestra estadísticas.

Variables de entorno:
  FILE_PATH      = "/app/wiki_es.txt"
  AMBASSADOR_URL = "http://localhost:5005"
  NUM_CHUNKS     = "3"
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
NUM_CHUNKS     = int(os.getenv("NUM_CHUNKS", "3"))

REQUEST_TIMEOUT = 600  # segundos por chunk


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
# PASO 2 — Dividir archivo en chunks por offsets de bytes
# ---------------------------------------------------------------------------

def calcular_chunks(file_path: str, n: int) -> list[tuple[int, int]]:
    total = os.path.getsize(file_path)
    size  = total // n
    chunks = []
    for i in range(n):
        start = i * size
        end   = start + size if i < n - 1 else total
        chunks.append((start, end))

    # Verificar cobertura exacta del archivo
    assert chunks[0][0] == 0,     "El primer chunk debe comenzar en 0"
    assert chunks[-1][1] == total, "El último chunk debe terminar en el byte final"
    for i in range(len(chunks) - 1):
        assert chunks[i][1] == chunks[i + 1][0], f"Brecha entre chunk {i} y {i+1}"

    _log(f"Archivo: {total:,} bytes → {n} chunks de ~{size:,} bytes c/u")
    return chunks


# ---------------------------------------------------------------------------
# PASO 3 — Enviar un chunk al Ambassador
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
# PASO 4 — Combinar resultados
# ---------------------------------------------------------------------------

def distribuido(file_path: str, ambassador_url: str, num_chunks: int) -> Counter:
    chunks = calcular_chunks(file_path, num_chunks)

    t_inicio = time.monotonic()
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

    # Combinar solo los chunks exitosos
    total_counter: Counter = Counter()
    exitosos = 0
    for cid, resp in resultados.items():
        if resp.get("status") == "ok":
            total_counter.update(resp.get("result", {}))
            exitosos += 1
        else:
            _log(f"chunk_{cid} fallido: {resp.get('reason', 'desconocido')}")

    # ---------------------------------------------------------------------------
    # PASO 5 — Mostrar estadísticas
    # ---------------------------------------------------------------------------
    elapsed = t_fin - t_inicio
    _log("=" * 60)
    _log(f"Chunks exitosos : {exitosos} / {num_chunks}")
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
    t0 = time.monotonic()
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
    # PASO 1: health check
    health_check()

    # PASOS 2-5: distribuido
    result_dist, t_dist = distribuido(FILE_PATH, AMBASSADOR_URL, NUM_CHUNKS)

    # PASO 6: ground truth secuencial
    result_seq, t_seq = sequential_count(FILE_PATH)

    _log("=" * 60)
    _log(f"Tiempo secuencial: {t_seq:.2f}s")

    if t_dist < t_seq:
        speedup = t_seq / t_dist
        _log(f"Speedup: {speedup:.2f}x más rápido")
    else:
        slowdown = t_dist / t_seq
        _log(f"Speedup: {slowdown:.2f}x más lento")
