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

    # --- Lectura del fragmento del archivo local ---
    try:
        # Modo "rb" (binario) en lugar de texto para poder hacer seek() exacto
        # a un offset de bytes y decodificar manualmente con errors="replace".
        # En modo texto Python podría ajustar offsets según BOM o fin de línea.
        with open(WIKI_PATH, "rb") as archivo:
            # Mover el cursor al byte de inicio del fragmento
            archivo.seek(start)

            # Leer exactamente (end - start) bytes
            fragmento_bytes = archivo.read(end - start)

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

    # --- Decodificación de bytes a texto ---
    # errors="replace" (no "ignore") evita que un corte en medio de un carácter
    # UTF-8 multi-byte (ej. vocal con tilde al borde del chunk) lance una excepción.
    # El byte inválido se reemplaza por U+FFFD ("?"), que el regex no captura
    # como letra y queda fuera del conteo — comportamiento correcto y seguro.
    texto = fragmento_bytes.decode("utf-8", errors="replace")

    # --- Conteo de palabras ---
    # findall devuelve una lista de todas las coincidencias del patrón.
    # .lower() normaliza para que "La" y "la" cuenten como la misma palabra.
    palabras = PATRON_PALABRAS.findall(texto.lower())
    conteo   = Counter(palabras)

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
