"""
worker.py — Servidor Flask que lee un rango de bytes del archivo local
y devuelve el conteo de palabras al Ambassador.

Endpoints:
  GET  /health  → estado del worker (uptime, accesibilidad del archivo, URL registrada)
  POST /count   → procesa el rango [start, end) y retorna {word: count, ...}

Variables de entorno:
  WORKER_ID      — (opcional) override manual; normalmente lo asigna el Ambassador
  PORT           — puerto de escucha (default: 5001)
  FILE_PATH      — ruta al archivo de texto (default: "/app/data/input.txt")
  AMBASSADOR_URL — URL del Ambassador para auto-registro (default: "")
  WORKER_URL     — URL propia para que el Ambassador llame de vuelta
  DELAY          — retardo artificial en segundos, para pruebas (default: 0)
  FAIL_MODE      — si "true", responde 500 siempre, para pruebas (default: false)
"""

import logging
import os
import re
import threading
import time
from collections import Counter

import requests
from flask import Flask, jsonify, request

import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[WORKER %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuración desde entorno (defaults desde config donde aplica)
# ---------------------------------------------------------------------------

PORT           = int(os.getenv("PORT", "5001"))
FILE_PATH      = os.getenv("FILE_PATH", "/app/data/input.txt")
AMBASSADOR_URL = os.getenv("AMBASSADOR_URL", "")
WORKER_URL     = os.getenv("WORKER_URL", f"http://localhost:{PORT}")

# El Ambassador asigna el ID al registrarse; antes del registro se usa un placeholder.
WORKER_ID      = os.getenv("WORKER_ID", "worker_sin_registrar")

DELAY     = float(os.getenv("DELAY", "0"))
FAIL_MODE = os.getenv("FAIL_MODE", "false").lower() == "true"

BUFFER_SIZE  = config.BUFFER_SIZE   # 64 MB por bloque de lectura
BOUNDARY_BUF = config.BOUNDARY_BUF  # Bytes extra para palabras en el límite de chunk

_start_time = time.monotonic()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """Estado del worker: uptime, accesibilidad del archivo y URL registrada."""
    uptime = round(time.monotonic() - _start_time, 1)
    try:
        file_accessible = os.path.isfile(FILE_PATH) and os.access(FILE_PATH, os.R_OK)
    except OSError:
        file_accessible = False

    return jsonify({
        "worker_id":       WORKER_ID,
        "status":          "ok",
        "file_path":       FILE_PATH,
        "file_accessible": file_accessible,
        "url_registrada":  WORKER_URL,
        "uptime_s":        uptime,
    })


# ---------------------------------------------------------------------------
# Lógica de conteo
# ---------------------------------------------------------------------------

def _ajustar_inicio(file, start: int) -> int:
    """Avanza hasta el primer espacio en blanco tras 'start' para no partir palabras."""
    if start == 0:
        return 0
    file.seek(start)
    while True:
        ch = file.read(1)
        if not ch or ch in " \t\n\r":
            return file.tell()


def _fin_palabra_parcial(texto: str) -> int:
    """
    Índice donde empieza la palabra parcial al final de 'texto', o len(texto)
    si el texto no termina dentro de una palabra.

    Se usa para arrastrar (carry-over) el fragmento final de un bloque al
    siguiente, evitando partir una palabra en la frontera de BUFFER_SIZE.
    """
    i = len(texto)
    while i > 0 and (texto[i - 1].isalnum() or texto[i - 1] == "_"):
        i -= 1
    return i


def count_words_from_file(file_path: str, start: int, end: int) -> Counter:
    """
    Lee el rango de bytes [start, end) del archivo y cuenta palabras.

    Ajusta el inicio al límite de palabra más cercano y extiende el fin
    BOUNDARY_BUF bytes para capturar la palabra partida en el corte 'end'.
    Lee en bloques de BUFFER_SIZE para evitar OOM en archivos grandes, y
    arrastra el fragmento de palabra que quede al final de cada bloque al
    siguiente, de modo que ninguna palabra se cuente partida en las fronteras
    internas de los bloques.

    NOTA: esta lógica debe mantenerse idéntica a coordinator._contar_secuencial,
    que la replica para el ground truth. Si cambia una, cambia la otra, o la
    verificación de correctitud dejará de cuadrar.
    """
    counter = Counter()

    with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
        real_start = _ajustar_inicio(file, start)
        file.seek(real_start)
        remaining = end - real_start
        carry     = ""   # Fragmento de palabra arrastrado del bloque anterior

        while remaining > 0:
            to_read = min(BUFFER_SIZE, remaining)
            block   = file.read(to_read)
            if not block:
                break
            remaining -= len(block.encode("utf-8", errors="ignore"))
            block = carry + block

            if remaining <= 0:
                # Último bloque del rango: extender hasta el siguiente espacio
                # para no partir la palabra que cruza 'end'.
                extra = file.read(BOUNDARY_BUF)
                corte = len(extra)
                for i, ch in enumerate(extra):
                    if ch in " \t\n\r":
                        corte = i
                        break
                block += extra[:corte]
                carry  = ""
            else:
                # Bloque intermedio: si termina dentro de una palabra, arrastrar
                # ese fragmento al siguiente bloque en vez de contarlo partido.
                pos   = _fin_palabra_parcial(block)
                carry = block[pos:]
                block = block[:pos]

            counter.update(re.findall(r"\b\w+\b", block.lower()))

        if carry:  # EOF inesperado con un fragmento pendiente
            counter.update(re.findall(r"\b\w+\b", carry.lower()))

    return counter


# ---------------------------------------------------------------------------
# Endpoint de conteo
# ---------------------------------------------------------------------------

@app.route("/count", methods=["POST"])
def count_words():
    """
    Procesa el rango de bytes indicado y retorna el conteo de palabras.

    Body esperado (JSON):
        start (int): Byte de inicio (inclusivo).
        end   (int): Byte de fin (exclusivo).
    """
    if FAIL_MODE:
        return jsonify({"worker_id": WORKER_ID, "status": "error", "reason": "simulated failure"}), 500

    if DELAY > 0:
        time.sleep(DELAY)

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"worker_id": WORKER_ID, "status": "error", "reason": "JSON inválido"}), 400

    start = data.get("start")
    end   = data.get("end")

    if start is None or end is None:
        return jsonify({"worker_id": WORKER_ID, "status": "error", "reason": "faltan start o end"}), 400

    start = int(start)
    end   = int(end)

    if start > end:
        return jsonify({
            "worker_id": WORKER_ID,
            "status":    "error",
            "reason":    f"start ({start}) > end ({end})",
        }), 400

    result = count_words_from_file(FILE_PATH, start, end)

    return jsonify({
        "worker_id": WORKER_ID,
        "start":     start,
        "end":       end,
        "result":    dict(result),
    })


# ---------------------------------------------------------------------------
# Registro dinámico con el Ambassador
# ---------------------------------------------------------------------------

_registered = threading.Event()


def _registration_loop(interval: float = 15.0) -> None:
    """
    Hilo daemon que mantiene el registro con el Ambassador.

    - Intenta registrarse indefinidamente hasta lograrlo.
    - Una vez registrado, hace ping periódico al Ambassador para detectar
      si se cayó. Si pierde contacto, vuelve a intentar registrarse.
    - Corre en background: el worker acepta requests en paralelo.
    """
    global WORKER_ID

    if not AMBASSADOR_URL:
        logger.info(f"[{WORKER_ID}] AMBASSADOR_URL no configurado — modo standalone")
        return

    register_url = f"{AMBASSADOR_URL}/register"
    health_url   = f"{AMBASSADOR_URL}/health"
    payload      = {"url": WORKER_URL}

    while True:
        # --- Fase 1: registrarse (reintenta para siempre) ---
        while not _registered.is_set():
            try:
                r = requests.post(register_url, json=payload, timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    WORKER_ID = data.get("worker_id", WORKER_ID)
                    logger.info(f"[{WORKER_ID}] Registrado en Ambassador: {WORKER_URL}")
                    _registered.set()
                    break
            except Exception as e:
                logger.warning(f"[{WORKER_ID}] Registro fallido: {e} — reintentando en {interval}s")
            time.sleep(interval)

        # --- Fase 2: vigilar conexión con Ambassador ---
        while _registered.is_set():
            time.sleep(interval)
            try:
                r = requests.get(health_url, timeout=3)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
            except Exception as e:
                logger.warning(f"[{WORKER_ID}] Ambassador inalcanzable: {e} — re-registrando")
                _registered.clear()
                break


if __name__ == "__main__":
    threading.Thread(target=_registration_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
