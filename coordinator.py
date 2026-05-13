"""
coordinator.py — Orquestador principal del contador de frecuencia distribuido.

Flujo de ejecución (6 pasos):
  1. Health check  — verifica que el Ambassador esté listo antes de enviar trabajo.
  2. Chunk division — divide wiki_es.txt en N rangos de bytes sin solapamiento.
  3. Dispatch       — envía cada chunk al Ambassador en paralelo con ThreadPoolExecutor.
  4. Merge          — combina los Counter de cada chunk en un único resultado global.
  5. Estadísticas   — imprime Top-20 palabras, chunks exitosos y tiempo distribuido.
  6. Speedup        — ejecuta el conteo secuencial y calcula el factor de aceleración.

El archivo wiki_es.txt (~5 GB) reside en cada máquina; nunca se transmite por red.
Los workers leen su rango de bytes directamente del disco local.

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
    """Imprime un mensaje con timestamp [COORD HH:MM:SS] al stdout sin buffering."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[COORD {ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# PASO 1 — Health check
# ---------------------------------------------------------------------------

def health_check() -> None:
    """
    Verifica que el Ambassador esté activo antes de enviar trabajo.

    Hace GET /health con timeout corto; aborta el proceso (SystemExit 1) si falla,
    porque sin Ambassador los chunks no llegarían a ningún worker.
    """
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
    """
    Divide file_path en n rangos de bytes contiguos y sin solapamiento.

    Args:
        file_path: Ruta al archivo a dividir.
        n:         Número de chunks deseados.

    Returns:
        Lista de tuplas (inicio, fin) en bytes, donde fin es exclusivo
        (igual al inicio del siguiente chunk).

    Raises:
        ValueError: Si los rangos no cubren exactamente el archivo completo.
    """
    total = os.path.getsize(file_path)
    size  = total // n  # tamaño base por división entera; el último chunk absorbe el resto
    chunks = []
    for i in range(n):
        start = i * size
        # El último chunk usa `total` en lugar de `start + size` para capturar
        # los bytes residuales de la división entera (total % n bytes extra).
        end   = start + size if i < n - 1 else total
        chunks.append((start, end))

    # Verificar cobertura exacta del archivo — assert se elimina con -O, usar raise
    if chunks[0][0] != 0 or chunks[-1][1] != total:
        raise ValueError("Los chunks no cubren el archivo completo")
    for i in range(len(chunks) - 1):
        if chunks[i][1] != chunks[i + 1][0]:
            raise ValueError(f"Brecha entre chunk {i} y {i+1}")

    _log(f"Archivo: {total:,} bytes → {n} chunks de ~{size:,} bytes c/u")
    return chunks


# ---------------------------------------------------------------------------
# PASO 3 — Enviar un chunk al Ambassador
# ---------------------------------------------------------------------------

def _enviar_chunk(chunk_id: int, inicio: int, fin: int) -> dict:
    """
    Envía un rango de bytes al Ambassador y devuelve su respuesta.

    Ejecutada en un hilo del ThreadPoolExecutor; los errores de red se capturan
    aquí para que el hilo no propague excepciones al pool.

    Args:
        chunk_id: Identificador numérico del chunk (0-based).
        inicio:   Byte de inicio (inclusivo).
        fin:      Byte de fin (exclusivo).

    Returns:
        Dict con al menos {"chunk_id", "status"}.
        En caso de error de red devuelve {"status": "error", "reason": <mensaje>}.
    """
    payload = {"chunk_id": chunk_id, "inicio": inicio, "fin": fin}
    _log(f"Enviando chunk_{chunk_id}: bytes {inicio:,} → {fin:,}")
    try:
        r = requests.post(
            f"{AMBASSADOR_URL}/dispatch",
            json=payload,
            # Con 3 workers y ~5 GB, cada chunk puede ser ~1.7 GB.
            # El Ambassador aplica reintentos internos, por eso el timeout es generoso (600 s).
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

def distribuido(file_path: str, num_chunks: int) -> tuple[Counter, float]:
    """
    Despacha todos los chunks en paralelo y combina los resultados.

    Crea un hilo por chunk (max_workers = num_chunks) para que todos los envíos
    al Ambassador ocurran simultáneamente. Fusiona los Counter de los chunks
    exitosos en un único Counter global.

    Args:
        file_path:  Ruta al archivo wiki_es.txt.
        num_chunks: Número de particiones en que se divide el archivo.

    Returns:
        Tupla (counter_global, tiempo_segundos).
        Si algún chunk falla, imprime advertencia y llama a SystemExit(1).
    """
    chunks = calcular_chunks(file_path, num_chunks)

    t_inicio = time.monotonic()
    resultados = {}

    with ThreadPoolExecutor(max_workers=num_chunks) as pool:
        # Diccionario futuro→chunk_id para poder recuperar el id si la respuesta
        # JSON no incluye "chunk_id" (p.ej. errores de red capturados en _enviar_chunk).
        futuros = {
            pool.submit(_enviar_chunk, i, start, end): i
            for i, (start, end) in enumerate(chunks)
        }
        # as_completed() itera los futuros en orden de llegada, no de envío,
        # lo que maximiza el aprovechamiento del hilo principal.
        for futuro in as_completed(futuros):
            resp = futuro.result()
            # Preferir chunk_id de la respuesta; si falta, usar el del mapa de futuros.
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

    if exitosos < num_chunks:
        _log(f"ADVERTENCIA: {num_chunks - exitosos} chunk(s) fallaron — resultado parcial")
        raise SystemExit(1)

    return total_counter, elapsed


# ---------------------------------------------------------------------------
# PASO 6 — Ground truth secuencial
# ---------------------------------------------------------------------------

# Solo letras del alfabeto español (incluyendo acentos y ü/ñ).
# Se usa en lugar de \w+ para excluir números, guiones y caracteres no latinos
# que aparecen en el texto wiki pero no son palabras del idioma.
PATRON_PALABRAS = re.compile(r"\b[a-záéíóúüñ]+\b")


def sequential_count(file_path: str) -> tuple[Counter, float]:
    """
    Cuenta palabras de forma secuencial en un único hilo (ground truth).

    Sirve como referencia T_seq para calcular el Speedup = T_seq / T_dist.
    Usa el mismo patrón de regex y la misma política de decodificación que
    los workers para que los resultados sean directamente comparables.

    Args:
        file_path: Ruta al archivo wiki_es.txt.

    Returns:
        Tupla (counter_palabras, tiempo_segundos).
    """
    _log("Iniciando conteo secuencial (ground truth) ...")
    t0 = time.monotonic()
    counter: Counter = Counter()
    # errors="replace" en lugar de "ignore": sustituye bytes inválidos por U+FFFD
    # en vez de descartarlos, reproduciendo exactamente el comportamiento de worker.py
    # y evitando diferencias artificiales en el conteo al comparar resultados.
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            words = PATRON_PALABRAS.findall(line.lower())
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
    result_dist, t_dist = distribuido(FILE_PATH, NUM_CHUNKS)

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
