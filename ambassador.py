"""
ambassador.py — Intermediario entre el Coordinator y los Workers.

Responsabilidades:
  - Recibir chunks del Coordinator (POST /dispatch).
  - Seleccionar un worker disponible mediante Round-Robin Inteligente:
      antes de despachar, consulta cb.allow_request(); si el worker
      está en OPEN lo salta automáticamente.
  - Reenviar el chunk al worker seleccionado (POST /count).
  - Gestionar fallos y reintentos (MAX_RETRIES intentos, rotando worker
      en cada reintento, sin delay entre ellos).
  - Registrar éxitos y fallos en el Circuit Breaker correspondiente.
  - Loggear cada evento con el formato exacto requerido para la demo.
  - Exponer el estado de todos los CBs en GET /workers/status.

Puerto: 5005
Modo: threaded=True — maneja peticiones del Coordinator en paralelo.

Política de exclusión por despacho:
  Cada llamada a /dispatch mantiene su propio conjunto `ya_fallaron`.
  Un worker excluido en un despacho sigue disponible para otros chunks
  que lleguen de forma concurrente; la exclusión NO es global.

Traducción de contrato (inicio/fin → start/end):
  El Coordinator habla español ({inicio, fin}); el Worker espera inglés
  ({start, end}). La traducción ocurre en _despachar_a_worker, manteniendo
  ambas interfaces estables sin modificar ninguno de los dos extremos.

Criterios de fallo (disparan cb.record_failure):
  - requests.Timeout
  - requests.ConnectionError
  - Respuesta HTTP con status_code != 200
  - JSON del worker con "status": "error"
"""

import itertools
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request

import config
from circuit_breaker_mock import CircuitBreaker, CircuitBreakerOpen

# ---------------------------------------------------------------------------
# Inicialización de la aplicación Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Circuit Breakers — uno por worker
# ---------------------------------------------------------------------------

# Diccionario: worker_id → instancia de CircuitBreaker
# Se crea una sola vez al arrancar el servidor; las peticiones concurrentes
# comparten estas instancias (cada CB es internamente thread-safe).
circuit_breakers: dict[str, CircuitBreaker] = {
    worker_id: CircuitBreaker(
        name=worker_id,
        fail_max=config.FAIL_MAX,
        reset_timeout=config.RESET_TIMEOUT,
    )
    for worker_id in config.WORKERS
}

# ---------------------------------------------------------------------------
# Round-Robin
# ---------------------------------------------------------------------------

# itertools.cycle produce la secuencia infinita: w1, w2, w3, w1, w2, w3, ...
# Se comparte entre hilos → sin el Lock, dos hilos podrían avanzar el cursor
# en paralelo y terminar eligiendo el mismo worker en el mismo instante.
_rr_cycle = itertools.cycle(config.WORKERS.keys())
_rr_lock  = threading.Lock()  # Protege el avance de _rr_cycle; solo se retiene durante next()


# ---------------------------------------------------------------------------
# Logging con formato demo
# ---------------------------------------------------------------------------

def _log(mensaje: str) -> None:
    """
    Imprime un mensaje de log con el formato requerido para la demo.

    Formato: [AMB HH:MM:SS] Mensaje

    Parámetros:
        mensaje (str): Texto del evento a registrar.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[AMB {timestamp}] {mensaje}", flush=True)


# ---------------------------------------------------------------------------
# Selección de worker (Round-Robin Inteligente)
# ---------------------------------------------------------------------------

def _seleccionar_worker(excluidos: set[str] | None = None) -> str | None:
    """
    Elige el próximo worker disponible usando Round-Robin Inteligente.

    "Inteligente" significa que antes de elegir un worker, consulta su Circuit
    Breaker. Si el CB está en OPEN (allow_request() devuelve False), ese worker
    se salta y se intenta con el siguiente en el ciclo.

    Se prueban como máximo len(WORKERS) candidatos para evitar un bucle infinito
    cuando todos los workers están en OPEN.

    Parámetros:
        excluidos (set[str] | None): IDs de workers que ya fallaron en este
            intento y no deben repetirse. Si es None se trata como conjunto vacío.

    Retorna:
        str  : ID del worker seleccionado (ej. "worker_1").
        None : Si ningún worker está disponible (todos en OPEN o en excluidos).
    """
    if excluidos is None:
        excluidos = set()

    total_workers = len(config.WORKERS)

    # Acotar el bucle a len(WORKERS) iteraciones garantiza terminación:
    # en el peor caso revisamos cada worker exactamente una vez antes de rendirse.
    for _ in range(total_workers):
        with _rr_lock:
            candidato = next(_rr_cycle)

        # Saltar workers que ya fallaron en este conjunto de reintentos
        if candidato in excluidos:
            continue

        cb = circuit_breakers[candidato]

        if cb.allow_request():
            return candidato

        # El CB está OPEN: logear el bloqueo y probar el siguiente
        _log(f"Estado CB {candidato:<12}: OPEN 🔴 — bloqueado")
        _log(f"Fast-fail           : intentando siguiente worker")

    # Ningún worker disponible
    return None


# ---------------------------------------------------------------------------
# Despacho al worker
# ---------------------------------------------------------------------------

def _despachar_a_worker(worker_id: str, payload: dict) -> dict:
    """
    Envía el chunk de trabajo al worker indicado y gestiona el resultado.

    En caso de éxito llama a cb.record_success().
    En caso de cualquier fallo llama a cb.record_failure() y lanza una
    excepción para que el bucle de reintentos lo maneje.

    Criterios de fallo:
      - requests.exceptions.Timeout       → timeout de red
      - requests.exceptions.ConnectionError → worker inaccesible
      - HTTP status_code != 200           → error del servidor
      - JSON con "status": "error"        → error lógico reportado por el worker

    Nota de traducción de contrato:
        El Coordinator envía {chunk_id, inicio, fin} al Ambassador.
        El Worker espera {start, end}. Esta función traduce el payload
        antes de reenviar, manteniendo ambas interfaces sin modificarlas.

    Parámetros:
        worker_id (str) : ID del worker destino (ej. "worker_1").
        payload   (dict): Cuerpo JSON recibido del Coordinator: {chunk_id, inicio, fin}.

    Retorna:
        dict: El campo "result" del JSON de respuesta del worker.

    Lanza:
        RuntimeError: En cualquiera de los criterios de fallo descritos.
    """
    url = f"{config.WORKERS[worker_id]}/count"
    cb  = circuit_breakers[worker_id]

    # Traducir de la interfaz del Coordinator (inicio/fin) a la del Worker (start/end).
    worker_payload = {"start": payload["inicio"], "end": payload["fin"]}

    try:
        respuesta = requests.post(
            url,
            json=worker_payload,
            timeout=config.REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        cb.record_failure()
        raise RuntimeError(f"Timeout al contactar a {worker_id}")

    except requests.exceptions.ConnectionError:
        cb.record_failure()
        raise RuntimeError(f"Sin conexión con {worker_id}")

    # Cualquier código HTTP que no sea 200 se trata como fallo
    if respuesta.status_code != 200:
        cb.record_failure()
        raise RuntimeError(
            f"{worker_id} respondió HTTP {respuesta.status_code}"
        )

    datos = respuesta.json()

    # El worker puede responder 200 pero indicar un error lógico en el JSON
    if datos.get("status") == "error":
        cb.record_failure()
        raise RuntimeError(
            f"{worker_id} reportó error: {datos.get('reason', 'desconocido')}"
        )

    # Llegamos aquí solo si todo fue bien
    cb.record_success()
    return datos.get("result", {})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/dispatch", methods=["POST"])
def dispatch():
    """
    Recibe un chunk del Coordinator y lo despacha a un worker disponible.

    Implementa la política de reintentos con rotación:
      - Intento 1: worker seleccionado por round-robin.
      - Intento 2: worker diferente al que falló (excluido del ciclo).
      - Intento 3: ídem, evitando los dos anteriores.
    Los reintentos son inmediatos (sin delay).

    Body esperado (JSON):
        chunk_id (int): Identificador del fragmento.
        inicio   (int): Byte de inicio del fragmento (inclusivo).
        fin      (int): Byte de fin del fragmento (exclusivo).

    Retorna (JSON exitoso):
        {"chunk_id": int, "worker_id": str, "status": "ok", "result": dict}

    Retorna (JSON de error):
        {"chunk_id": int|None, "worker_id": null, "status": "error", "reason": str}
    """
    datos = request.get_json(silent=True)
    if not datos:
        return jsonify({
            "chunk_id":  None,
            "worker_id": None,
            "status":    "error",
            "reason":    "cuerpo JSON inválido o ausente",
        }), 400

    chunk_id = datos.get("chunk_id")
    inicio   = datos.get("inicio")
    fin      = datos.get("fin")

    if chunk_id is None or inicio is None or fin is None:
        return jsonify({
            "chunk_id":  chunk_id,
            "worker_id": None,
            "status":    "error",
            "reason":    "faltan campos: chunk_id, inicio, fin",
        }), 400

    _log(f"Chunk recibido      : chunk_{chunk_id} (bytes {inicio} → {fin})")

    payload    = {"chunk_id": chunk_id, "inicio": inicio, "fin": fin}
    # Conjunto local a este despacho: excluye workers fallidos sin afectar otros chunks concurrentes.
    ya_fallaron: set[str] = set()

    for intento in range(1, config.MAX_RETRIES + 1):

        # Seleccionar worker disponible (excluyendo los que ya fallaron)
        worker_id = _seleccionar_worker(excluidos=ya_fallaron)

        if worker_id is None:
            _log("Todos los workers OPEN — abortando chunk")
            return jsonify({
                "chunk_id":  chunk_id,
                "worker_id": None,
                "status":    "error",
                "reason":    "all_workers_open",
            }), 503

        cb = circuit_breakers[worker_id]
        # Capturar ANTES del call: si la prueba tiene éxito, record_success()
        # transiciona el CB a CLOSED y el estado original se perdería.
        era_half_open = cb.state.value == "HALF_OPEN"
        _log(f"Worker seleccionado : {worker_id}")
        _log(f"Estado CB {worker_id:<12}: {cb.state.value} {'🟢' if cb.state.value == 'CLOSED' else '🟡' if cb.state.value == 'HALF_OPEN' else '🔴'}")
        _log(f"Intento             : {intento} / {config.MAX_RETRIES}")

        try:
            t_inicio  = time.monotonic()
            resultado = _despachar_a_worker(worker_id, payload)
            t_fin     = time.monotonic()

            _log(f"Tiempo de respuesta : {t_fin - t_inicio:.3f}s")
            _log(f"Resultado           : OK ✓")

            # Detectar transición HALF_OPEN → CLOSED (prueba exitosa de recuperación)
            if era_half_open:
                _log(f"Prueba exitosa      : CB {worker_id} → CLOSED 🟢")

            return jsonify({
                "chunk_id":  chunk_id,
                "worker_id": worker_id,
                "status":    "ok",
                "result":    resultado,
            })

        except RuntimeError as error:
            _log(f"Fallo en {worker_id}    : {error}")
            ya_fallaron.add(worker_id)

            if intento < config.MAX_RETRIES:
                _log(f"Reintentando        : intento {intento + 1} / {config.MAX_RETRIES}")

    # Se agotaron todos los reintentos sin éxito
    _log(f"Max reintentos agotados para chunk_{chunk_id}")
    return jsonify({
        "chunk_id":  chunk_id,
        "worker_id": None,
        "status":    "error",
        "reason":    "max_retries_exceeded",
    }), 502


@app.route("/workers/status", methods=["GET"])
def workers_status():
    """
    Retorna el estado actual de todos los Circuit Breakers.

    Útil para monitorear la salud del sistema durante la demo.

    Retorna (JSON):
        {
          "activos": ["worker_1", ...],   # workers en estado CLOSED
          "n": int,                        # total de workers activos
          "detalle": { worker_id: {name, state, fail_count, seconds_open} }
        }
    """
    detalle = {wid: cb.get_status() for wid, cb in circuit_breakers.items()}
    activos = [wid for wid, info in detalle.items() if info["state"] == "CLOSED"]

    return jsonify({
        "activos": activos,
        "n":       len(activos),
        "detalle": detalle,
    })


@app.route("/health", methods=["GET"])
def health():
    """
    Health check del Ambassador — confirma que el servidor está vivo.

    Retorna:
        JSON: {"status": "ok", "service": "ambassador"}
    """
    return jsonify({"status": "ok", "service": "ambassador"})


# ---------------------------------------------------------------------------
# Health Poller — monitoreo proactivo de workers
# ---------------------------------------------------------------------------

def _health_poller() -> None:
    """
    Hilo de fondo que monitorea el estado de cada worker cada RESET_TIMEOUT segundos.

    Llama a GET /health en cada worker y actualiza su Circuit Breaker:
      - Respuesta 200      → cb.record_success()  (cierra HALF_OPEN si el worker se recuperó)
      - Timeout / error    → cb.record_failure()  (puede abrir el CB antes del primer chunk)

    El intervalo es igual a RESET_TIMEOUT para que el poller esté sincronizado
    con el ciclo de recuperación del CB: cuando el CB pasa a HALF_OPEN, el
    siguiente poll confirma si el worker ya está sano.

    Solo loguea cuando el estado del CB cambia, para no saturar la consola.
    Corre como daemon thread: se detiene automáticamente cuando Flask termina.
    """
    while True:
        time.sleep(config.RESET_TIMEOUT)   # esperar primero; al arrancar los workers aún no están listos
        for worker_id, url in config.WORKERS.items():
            cb      = circuit_breakers[worker_id]
            estado_antes = cb.state

            try:
                r = requests.get(f"{url}/health", timeout=2)
                if r.status_code == 200:
                    # allow_request() activa OPEN→HALF_OPEN si el timeout expiró.
                    # Sin esta llamada, un CB en OPEN nunca transiciona desde el poller.
                    if cb.allow_request():
                        cb.record_success()   # HALF_OPEN→CLOSED  o  CLOSED reset fail_count
                else:
                    cb.record_failure()
            except requests.exceptions.ConnectionError:
                # ConnectionError = el worker no está corriendo. Registrar fallo real.
                cb.record_failure()
            except Exception:
                # Timeout u otro error: el worker puede estar ocupado procesando un chunk.
                # No penalizar el CB — un worker lento no es un worker caído.
                pass

            # Loguear solo si el estado cambió (evita ruido en consola durante el demo)
            estado_despues = cb.state
            if estado_antes != estado_despues:
                icono = "🟢" if estado_despues.value == "CLOSED" else "🟡" if estado_despues.value == "HALF_OPEN" else "🔴"
                _log(f"[POLL] CB {worker_id}: {estado_antes.value} → {estado_despues.value} {icono}")


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _log("Iniciando Ambassador")
    _log(f"Workers configurados: {list(config.WORKERS.keys())}")
    _log(f"FAIL_MAX={config.FAIL_MAX} | RESET_TIMEOUT={config.RESET_TIMEOUT}s | MAX_RETRIES={config.MAX_RETRIES}")

    # Arrancar el health poller como daemon thread antes de Flask.
    # daemon=True: el hilo se detiene automáticamente cuando el proceso principal termina.
    poller = threading.Thread(target=_health_poller, daemon=True, name="health-poller")
    poller.start()
    _log(f"Health poller iniciado (intervalo: {config.RESET_TIMEOUT}s)")

    # threaded=True: Flask crea un hilo por petición, permitiendo que el
    # Coordinator envíe múltiples chunks en paralelo sin que se encolen.
    app.run(
        host=config.AMBASSADOR_HOST,
        port=config.AMBASSADOR_PORT,
        threaded=True,
    )
