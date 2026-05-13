from flask import Flask, request, jsonify
from collections import Counter
import re
import os
import time
import requests

app = Flask(__name__)

WORKER_ID      = os.getenv("WORKER_ID", "worker_1")
PORT           = int(os.getenv("PORT", "5001"))
FILE_PATH      = os.getenv("FILE_PATH", "/app/data/input.txt")
AMBASSADOR_URL = os.getenv("AMBASSADOR_URL", "")
WORKER_URL     = os.getenv("WORKER_URL", f"http://localhost:{PORT}")

DELAY     = float(os.getenv("DELAY", "0"))
FAIL_MODE = os.getenv("FAIL_MODE", "false").lower() == "true"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "worker_id": WORKER_ID,
        "status":    "ok",
        "file_path": FILE_PATH,
    })


BUFFER_SIZE  = 64 * 1024 * 1024  # 64 MB por bloque
BOUNDARY_BUF = 512               # Bytes extra para detectar límite de palabra


def _ajustar_inicio(file, start: int) -> int:
    if start == 0:
        return 0
    file.seek(start)
    while True:
        ch = file.read(1)
        if not ch or ch in " \t\n\r":
            return file.tell()


def count_words_from_file(file_path, start, end):
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


@app.route("/count", methods=["POST"])
def count_words():
    if FAIL_MODE:
        return jsonify({"worker_id": WORKER_ID, "error": "simulated failure"}), 500

    if DELAY > 0:
        time.sleep(DELAY)

    data  = request.get_json()
    start = int(data.get("start", 0))
    end   = int(data.get("end", 0))

    result = count_words_from_file(FILE_PATH, start, end)

    return jsonify({
        "worker_id": WORKER_ID,
        "start":  start,
        "end":    end,
        "result": dict(result),
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
        print(f"[WORKER {WORKER_ID}] AMBASSADOR_URL no configurado — modo standalone", flush=True)
        return

    url     = f"{AMBASSADOR_URL}/register"
    payload = {"worker_id": WORKER_ID, "url": WORKER_URL}

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(url, json=payload, timeout=5)
            if r.status_code == 200:
                print(f"[WORKER {WORKER_ID}] Registrado en Ambassador: {WORKER_URL}", flush=True)
                return
        except Exception as e:
            print(f"[WORKER {WORKER_ID}] Intento {attempt}/{max_attempts} fallido: {e}", flush=True)
        time.sleep(delay)

    print(f"[WORKER {WORKER_ID}] No se pudo registrar tras {max_attempts} intentos", flush=True)


if __name__ == "__main__":
    register_with_ambassador()
    app.run(host="0.0.0.0", port=PORT)
