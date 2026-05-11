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


BUFFER_SIZE  = 64 * 1024 * 1024  # 64 MB por bloque — evita OOM con chunks de varios GB
BOUNDARY_BUF = 512               # Bytes extra a leer para detectar límite de palabra


def _ajustar_inicio(file, start: int) -> int:
    """
    Si start > 0, avanza hasta el primer espacio en blanco.

    El chunk anterior es responsable de la palabra que cruza el límite:
    él leerá hasta el espacio. Este chunk la salta para no contarla doble.

    Retorna el start ajustado (posición después del espacio).
    """
    if start == 0:
        return 0
    file.seek(start)
    while True:
        ch = file.read(1)
        if not ch or ch in " \t\n\r":
            return file.tell()


def count_words_from_file(file_path, start, end):
    """
    Cuenta palabras en el rango de bytes [start, end) del archivo,
    alineando los límites a fronteras de palabra para evitar fragmentos.

    Estrategia de límites:
      - Inicio (start > 0): avanza hasta el primer espacio — salta el
        fragmento inicial que pertenece al chunk anterior.
      - Final: lee hasta BOUNDARY_BUF bytes extra más allá de end para
        completar la palabra que cruza el límite. El chunk siguiente la
        saltará con el ajuste de inicio.

    Lee en bloques de BUFFER_SIZE para mantener el uso de RAM acotado.

    Parámetros:
        file_path (str): Ruta al archivo de texto.
        start     (int): Byte de inicio nominal (puede ajustarse hacia adelante).
        end       (int): Byte de fin nominal (puede extenderse hasta el próximo espacio).

    Retorna:
        Counter: Frecuencia de cada palabra en el rango ajustado.
    """
    counter = Counter()

    with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
        real_start = _ajustar_inicio(file, start)
        file.seek(real_start)
        remaining = end - real_start

        while remaining > 0:
            to_read = min(BUFFER_SIZE, remaining)
            chunk = file.read(to_read)
            if not chunk:
                break
            remaining -= len(chunk.encode("utf-8", errors="ignore"))

            # Último bloque: leer caracteres extra hasta el próximo espacio
            # para capturar completa la palabra que cruza el límite de fin.
            if remaining <= 0:
                extra = file.read(BOUNDARY_BUF)
                for i, ch in enumerate(extra):
                    if ch in " \t\n\r":
                        chunk += extra[:i]
                        break
                else:
                    chunk += extra  # llegamos al EOF sin encontrar espacio

            words = re.findall(r"\b\w+\b", chunk.lower())
            counter.update(words)

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