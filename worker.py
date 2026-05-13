"""
worker.py — Servidor HTTP que cuenta frecuencia de palabras en un rango del archivo.

Responsabilidades:
  - Recibir un rango de bytes (start, end) desde el Ambassador.
  - Leer ese fragmento del archivo wiki_es.txt almacenado localmente
    (el archivo NUNCA viaja por red — cada laptop tiene su propia copia).
  - Limpiar el texto y contar palabras con collections.Counter.
  - Responder con el diccionario de frecuencias.

Nota: el Ambassador traduce los campos del Coordinator (inicio/fin → start/end)
antes de reenviar la petición aquí.

Puerto: 5001
"""

from flask import Flask, request, jsonify
from collections import Counter
import re
import os
import time

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuración por variables de entorno
# ---------------------------------------------------------------------------

# Identidad del worker — debe ser única por laptop (worker_1, worker_2, worker_3)
WORKER_ID = os.getenv("WORKER_ID", "worker_1")

# Ruta al archivo de texto dentro del contenedor Docker
WIKI_PATH = os.getenv("WIKI_PATH", "/app/wiki_es.txt")

# DELAY: segundos de latencia artificial para probar que el Circuit Breaker
# detecta timeouts y abre el circuito ante respuestas lentas.
DELAY = float(os.getenv("DELAY", "0"))

# FAIL_MODE: si es "true", cada petición devuelve HTTP 500 para disparar
# el contador de fallos del Circuit Breaker y forzar la transición CLOSED→OPEN.
FAIL_MODE = os.getenv("FAIL_MODE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Regex para extraer palabras
# ---------------------------------------------------------------------------

# Compilado a nivel de módulo (no dentro de la función) para que el motor de
# expresiones regulares lo procese una sola vez al arrancar, en lugar de
# recompilarlo en cada petición — mejora el rendimiento bajo carga alta.
# \b = word boundary; [a-záéíóúüñ]+ = solo letras españolas (tildes, ñ, ü incluidos).
PATRON_PALABRAS = re.compile(r"\b[a-záéíóúüñ]+\b")

# Tamaño de bloque para la lectura del archivo. Leer el chunk completo (1.7 GB)
# en un solo read() agota la RAM cuando 3 hilos lo hacen en paralelo (~5 GB).
# Con bloques de 64 MB el pico de RAM por hilo se limita a ~64 MB.
BUFFER_SIZE = 64 * 1024 * 1024  # 64 MB


def _contar_palabras_en_rango(file_path: str, start: int, end: int) -> Counter:
    """
    Lee el rango de bytes [start, end) en bloques de BUFFER_SIZE y cuenta palabras.

    Evita OOM al procesar chunks grandes (~1.7 GB) en paralelo: en lugar de
    cargar el chunk completo en memoria, procesa un bloque de 64 MB a la vez.

    Parámetros:
        file_path: Ruta al archivo wiki_es.txt.
        start:     Byte de inicio (inclusivo).
        end:       Byte de fin (exclusivo).

    Retorna:
        Counter con la frecuencia de cada palabra en el rango.
    """
    counter = Counter()
    with open(file_path, "rb") as f:
        f.seek(start)
        restante = end - start
        while restante > 0:
            bloque = f.read(min(BUFFER_SIZE, restante))
            if not bloque:
                break
            restante -= len(bloque)
            # errors="replace": bytes inválidos → U+FFFD, que el regex no captura
            texto = bloque.decode("utf-8", errors="replace")
            counter.update(PATRON_PALABRAS.findall(texto.lower()))
    return counter


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """
    Health check — el Ambassador lo usa para saber si el worker está vivo.

    Retorna:
        JSON: {"status": "ok", "worker_id": "<id>"}
    """
    return jsonify({
        "status": "ok",
        "worker_id": WORKER_ID
    })


@app.route("/count", methods=["POST"])
def count_words():
    """
    Cuenta la frecuencia de palabras en el rango de bytes indicado.

    El archivo wiki_es.txt se abre localmente — el texto NO viaja por red.
    El coordinator calcula los offsets de bytes y los envía al Ambassador,
    que a su vez los reenvía aquí.

    Body esperado (JSON):
        start (int): offset de byte donde empieza el fragmento
        end   (int): offset de byte donde termina el fragmento (no inclusivo)

    Retorna:
        JSON exitoso : {"worker_id": str, "status": "ok", "result": {palabra: frecuencia}}
        JSON de error: {"worker_id": str, "status": "error", "reason": str}
    """

    # Modo falla — simula un worker caído para probar el Circuit Breaker
    if FAIL_MODE:
        return jsonify({
            "worker_id": WORKER_ID,
            "status": "error",
            "reason": "simulated failure"
        }), 500

    # Delay artificial para simular latencia de red o procesamiento lento
    if DELAY > 0:
        time.sleep(DELAY)

    # --- Validación del cuerpo de la petición ---
    datos = request.get_json(silent=True)
    if not datos:
        return jsonify({
            "worker_id": WORKER_ID,
            "status": "error",
            "reason": "cuerpo JSON inválido o ausente"
        }), 400

    start = datos.get("start")
    end   = datos.get("end")

    # Verificar que los campos obligatorios estén presentes
    if start is None or end is None:
        return jsonify({
            "worker_id": WORKER_ID,
            "status": "error",
            "reason": "faltan campos requeridos: start, end"
        }), 400

    # Verificar que el rango sea válido
    if start < 0 or end <= start:
        return jsonify({
            "worker_id": WORKER_ID,
            "status": "error",
            "reason": f"rango inválido: start={start}, end={end}"
        }), 400

    # --- Conteo por bloques (evita OOM con chunks de ~1.7 GB) ---
    try:
        conteo = _contar_palabras_en_rango(WIKI_PATH, start, end)
    except FileNotFoundError:
        return jsonify({
            "worker_id": WORKER_ID,
            "status": "error",
            "reason": f"archivo no encontrado: {WIKI_PATH}"
        }), 500
    except OSError as e:
        return jsonify({
            "worker_id": WORKER_ID,
            "status": "error",
            "reason": f"error al leer el archivo: {str(e)}"
        }), 500

    return jsonify({
        "worker_id": WORKER_ID,
        "status": "ok",
        "result": dict(conteo)   # Counter no es serializable a JSON directamente
    })


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    puerto = int(os.getenv("PORT", "5001"))
    # host="0.0.0.0" acepta conexiones desde cualquier IP de la red local,
    # no solo desde localhost — necesario para Docker y red WiFi compartida.
    app.run(host="0.0.0.0", port=puerto)
