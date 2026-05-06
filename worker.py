from flask import Flask, request, jsonify
from collections import Counter
import re
import os
import time

app = Flask(__name__)

WORKER_ID = os.getenv("WORKER_ID", "worker_1")
DELAY = float(os.getenv("DELAY", "0"))
FAIL_MODE = os.getenv("FAIL_MODE", "false").lower() == "true"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "worker_id": WORKER_ID,
        "status": "ok"
    })


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
    text = data.get("text", "")

    words = re.findall(r"\b\w+\b", text.lower())
    word_count = Counter(words)

    return jsonify({
        "worker_id": WORKER_ID,
        "result": dict(word_count)
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)