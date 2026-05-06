"""
worker.py — Servidor HTTP que cuenta frecuencia de palabras en un rango del archivo.

Responsabilidades:
  - Recibir un rango de bytes (inicio, fin) desde el Ambassador.
  - Leer ese fragmento del archivo wiki_es.txt almacenado localmente.
  - Limpiar el texto y contar palabras con collections.Counter.
  - Responder con el diccionario de frecuencias.

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

# Segundos de delay artificial — útil para simular latencia en pruebas
DELAY = float(os.getenv("DELAY", "0"))

# Si es "true", el worker responde siempre con error — útil para probar el Circuit Breaker
FAIL_MODE = os.getenv("FAIL_MODE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Regex para extraer palabras
# ---------------------------------------------------------------------------

# Captura secuencias de letras (incluye tildes, ñ y diéresis del español).
# \b es word boundary; [a-záéíóúüñ]+ captura solo letras, sin números ni signos.
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
        chunk_id (int): identificador del fragmento para trazabilidad
        inicio   (int): offset de byte donde empieza el fragmento
        fin      (int): offset de byte donde termina el fragmento (no inclusivo)

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

    chunk_id = datos.get("chunk_id")
    inicio   = datos.get("inicio")
    fin      = datos.get("fin")

    # Verificar que los tres campos obligatorios estén presentes
    if inicio is None or fin is None or chunk_id is None:
        return jsonify({
            "worker_id": WORKER_ID,
            "status": "error",
            "reason": "faltan campos requeridos: chunk_id, inicio, fin"
        }), 400

    # Verificar que el rango sea válido
    if inicio < 0 or fin <= inicio:
        return jsonify({
            "worker_id": WORKER_ID,
            "status": "error",
            "reason": f"rango inválido: inicio={inicio}, fin={fin}"
        }), 400

    # --- Lectura del fragmento del archivo local ---
    try:
        with open(WIKI_PATH, "rb") as archivo:
            # Mover el cursor al byte de inicio del fragmento
            archivo.seek(inicio)

            # Leer exactamente (fin - inicio) bytes
            fragmento_bytes = archivo.read(fin - inicio)

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
    # errors="replace" evita que un corte en medio de un carácter UTF-8
    # multi-byte (ej. una vocal con tilde) lance una excepción.
    # El carácter corrupto se reemplaza por "?" y se ignora en el conteo.
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
