from flask import Flask, request, jsonify
from collections import Counter
import re
import os
import time

app = Flask(__name__)

WORKER_ID = os.getenv("WORKER_ID", "worker_1")
PORT = int(os.getenv("PORT", "5001"))
FILE_PATH = os.getenv("FILE_PATH", "/app/data/input.txt")

DELAY = float(os.getenv("DELAY", "0"))
FAIL_MODE = os.getenv("FAIL_MODE", "false").lower() == "true"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "worker_id": WORKER_ID,
        "status": "ok",
        "file_path": FILE_PATH
    })


BUFFER_SIZE = 64 * 1024 * 1024  # 64 MB por bloque — evita OOM con chunks de varios GB


def count_words_from_file(file_path, start, end):
    """
    Cuenta palabras en el rango de bytes [start, end) del archivo.

    Lee en bloques de BUFFER_SIZE en vez de cargar el chunk completo en memoria.
    Esto permite procesar chunks de varios GB con uso de RAM acotado (~64 MB por hilo).

    Parámetros:
        file_path (str): Ruta al archivo de texto.
        start     (int): Byte de inicio (inclusivo).
        end       (int): Byte de fin (exclusivo).

    Retorna:
        Counter: Frecuencia de cada palabra en el rango dado.
    """
    counter = Counter()

    with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
        file.seek(start)
        remaining = end - start

        while remaining > 0:
            to_read = min(BUFFER_SIZE, remaining)
            chunk = file.read(to_read)
            if not chunk:
                break
            words = re.findall(r"\b\w+\b", chunk.lower())
            counter.update(words)
            remaining -= len(chunk.encode("utf-8", errors="ignore"))

    return counter


@app.route("/count", methods=["POST"])
def count_words():
    if FAIL_MODE:
        return jsonify({
            "worker_id": WORKER_ID,
            "error": "simulated failure"
        }), 500

    if DELAY > 0:
        time.sleep(DELAY)

    data = request.get_json()

    start = int(data.get("start", 0))
    end = int(data.get("end", 0))

    result = count_words_from_file(FILE_PATH, start, end)

    return jsonify({
        "worker_id": WORKER_ID,
        "start": start,
        "end": end,
        "result": dict(result)
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)