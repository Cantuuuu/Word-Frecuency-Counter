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


def count_words_from_file(file_path, start, end):
    counter = Counter()

    with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
        file.seek(start)

        size = end - start
        chunk = file.read(size)

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