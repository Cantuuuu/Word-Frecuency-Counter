"""
worker.py — Servidor Flask que lee un rango de bytes del archivo local
y devuelve el conteo de palabras al Ambassador.

Endpoints:
  GET  /health  → estado del worker (uptime, accesibilidad del archivo, URL registrada)
  POST /count   → procesa el rango [start, end) y retorna {word: count, ...}

Variables de entorno:
  WORKER_ID      — identificador único (default: "worker_1")
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

WORKER_ID      = os.getenv("WORKER_ID", "worker_1")
PORT           = int(os.getenv("PORT", "5001"))
FILE_PATH      = os.getenv("FILE_PATH", "/app/data/input.txt")
AMBASSADOR_URL = os.getenv("AMBASSADOR_URL", "")
WORKER_URL     = os.getenv("WORKER_URL", f"http://localhost:{PORT}")

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


def count_words_from_file(file_path: str, start: int, end: int) -> Counter:
    """
    Lee el rango de bytes [start, end) del archivo y cuenta palabras.

    Ajusta el inicio al límite de palabra más cercano y extiende el fin
    BOUNDARY_BUF bytes para capturar palabras partidas en el corte.
    Lee en bloques de BUFFER_SIZE para evitar OOM en archivos grandes.
    """
    counter = Counter()

    with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
        real_start = _ajustar_inicio(file, start)
        file.seek(real_start)
        remaining = end - real_start

        while remaining > 0:
            to_read = min(BUFFER_SIZE, remaining)
            chunk   = file.read(to_read)
            if not chunk:
                break
            remaining -= len(chunk.encode("utf-8", errors="ignore"))

            if remaining <= 0:
                extra = file.read(BOUNDARY_BUF)
                for i, ch in enumerate(extra):
                    if ch in " \t\n\r":
                        chunk += extra[:i]
                        break
                else:
                    chunk += extra

            words = re.findall(r"\b\w+\b", chunk.lower())
            counter.update(words)

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

def register_with_ambassador(max_attempts: int = 15, delay: float = 2.0) -> None:
    """
    Anuncia este worker al Ambassador para entrar al pool dinámico.
    Reintenta hasta max_attempts veces (el Ambassador puede no estar listo aún).
    """
    if not AMBASSADOR_URL:
        logger.info(f"[{WORKER_ID}] AMBASSADOR_URL no configurado — modo standalone")
        return

    url     = f"{AMBASSADOR_URL}/register"
    payload = {"worker_id": WORKER_ID, "url": WORKER_URL}

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(url, json=payload, timeout=5)
            if r.status_code == 200:
                logger.info(f"[{WORKER_ID}] Registrado en Ambassador: {WORKER_URL}")
                return
        except Exception as e:
            logger.warning(f"[{WORKER_ID}] Intento {attempt}/{max_attempts} fallido: {e}")
        time.sleep(delay)

    logger.error(f"[{WORKER_ID}] No se pudo registrar tras {max_attempts} intentos")


if __name__ == "__main__":
    register_with_ambassador()
    app.run(host="0.0.0.0", port=PORT)
