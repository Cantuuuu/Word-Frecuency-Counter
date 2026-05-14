"""
ambassador.py — Intermediario entre el Coordinator y los Workers.

Responsabilidades:
  - Registrar workers dinámicamente (POST /register).
  - Dar de baja workers manualmente (DELETE /workers/<id>).
  - Recibir chunks del Coordinator (POST /dispatch).
  - Seleccionar un worker disponible mediante Round-Robin Inteligente.
  - Reenviar el chunk al worker seleccionado (POST /count).
  - Gestionar fallos con Circuit Breaker por worker.
  - Evitar doble-asignación: no enviar un chunk a un worker ya ocupado.
  - Persistir el registro de workers para sobrevivir reinicios.
  - Exponer estado en GET /workers/status.

Puerto: 5005
"""

import json
import logging
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

import config
from circuit_breaker import CircuitBreaker, CircuitBreakerOpen

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[AMB %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Aplicación Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Registro dinámico de workers
# ---------------------------------------------------------------------------

# worker_id → URL base (ej. "http://148.220.1.1:5001")
_workers: dict[str, str] = {}
_workers_lock = threading.Lock()

# Circuit Breakers — uno por worker, creado al registrarse
circuit_breakers: dict[str, CircuitBreaker] = {}

# ---------------------------------------------------------------------------
# Seguimiento de workers ocupados (anti-doble-asignación)
# ---------------------------------------------------------------------------

# Un worker ocupado no recibirá otro chunk hasta que termine el actual.
_busy_workers: set[str] = set()
_busy_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Round-Robin dinámico
# ---------------------------------------------------------------------------

_rr_index = 0
_rr_lock  = threading.Lock()

# ---------------------------------------------------------------------------
# Persistencia del registro de workers
# ---------------------------------------------------------------------------

_REGISTRY_FILE = "workers_registry.json"


def _save_registry() -> None:
    """Persiste el diccionario de workers a disco en formato JSON."""
    with _workers_lock:
        data = dict(_workers)
    try:
        with open(_REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        logger.warning(f"No se pudo guardar registro: {e}")


def _load_registry() -> None:
    """
    Carga el registro de workers persistido al arrancar el Ambassador.

    Los Circuit Breakers se crean en estado CLOSED para cada worker cargado.
    Si un worker ya no está disponible, el CB lo detectará en el primer fallo.
    """
    if not os.path.exists(_REGISTRY_FILE):
        return
    try:
        with open(_REGISTRY_FILE, encoding="utf-8") as f:
            data: dict = json.load(f)
        with _workers_lock:
            for worker_id, url in data.items():
                _workers[worker_id] = url
                if worker_id not in circuit_breakers:
                    circuit_breakers[worker_id] = CircuitBreaker(
                        name=worker_id,
                        fail_max=config.FAIL_MAX,
                        reset_timeout=config.RESET_TIMEOUT,
                    )
        logger.info(f"Registro cargado desde disco: {list(data.keys())}")
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"No se pudo cargar registro: {e}")


# ---------------------------------------------------------------------------
# Selección de worker (Round-Robin Inteligente)
# ---------------------------------------------------------------------------

def _seleccionar_worker(excluidos: set[str] | None = None) -> str | None:
    """
    Elige el próximo worker disponible usando Round-Robin sobre el pool
    dinámico actual. Salta workers que:
      - estén en el conjunto de excluidos (ya fallaron en este intento),
      - estén ocupados procesando otro chunk, o
      - tengan su Circuit Breaker en OPEN.

    Retorna None si no hay ningún worker disponible.
    """
    global _rr_index

    if excluidos is None:
        excluidos = set()

    with _workers_lock:
        worker_ids = list(_workers.keys())

    total = len(worker_ids)
    if total == 0:
        return None

    for _ in range(total):
        with _rr_lock:
            idx       = _rr_index % total
            _rr_index += 1
            candidato = worker_ids[idx]

        if candidato in excluidos:
            continue

        with _busy_lock:
            if candidato in _busy_workers:
                logger.info(f"Worker {candidato:<12}: ocupado — saltando")
                continue

        cb = circuit_breakers.get(candidato)
        if cb and cb.allow_request():
            return candidato

        logger.info(f"CB {candidato:<12}: OPEN — fast-fail, intentando siguiente")

    return None


# ---------------------------------------------------------------------------
# Despacho al worker
# ---------------------------------------------------------------------------

def _despachar_a_worker(worker_id: str, payload: dict) -> dict:
    """
    Envía el chunk al worker indicado, traduciendo el contrato:
        Coordinator envía  {chunk_id, inicio, fin}
        Worker espera      {start, end}

    Marca el worker como ocupado durante toda la llamada HTTP y lo
    libera en el bloque finally. Registra éxito/fallo en su Circuit Breaker.

    Lanza RuntimeError ante cualquier fallo para que dispatch() lo maneje.
    """
    with _workers_lock:
        url_base = _workers.get(worker_id)

    if not url_base:
        raise RuntimeError(f"Worker {worker_id} no registrado")

    url = f"{url_base}/count"
    cb  = circuit_breakers[worker_id]

    worker_payload = {"start": payload["inicio"], "end": payload["fin"]}

    with _busy_lock:
        _busy_workers.add(worker_id)

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
    finally:
        with _busy_lock:
            _busy_workers.discard(worker_id)

    if respuesta.status_code != 200:
        cb.record_failure()
        raise RuntimeError(f"{worker_id} respondió HTTP {respuesta.status_code}")

    datos = respuesta.json()

    if datos.get("status") == "error":
        cb.record_failure()
        raise RuntimeError(f"{worker_id} reportó error: {datos.get('reason', 'desconocido')}")

    cb.record_success()
    return datos.get("result", {})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/register", methods=["POST"])
def register():
    """
    Registra un worker en el pool dinámico y persiste el registro a disco.

    Body esperado (JSON):
        worker_id (str): Identificador único del worker (ej. "worker_1").
        url       (str): URL base accesible del worker (ej. "http://148.x.x.x:5001").
    """
    datos = request.get_json(silent=True)
    if not datos:
        return jsonify({"status": "error", "reason": "JSON inválido"}), 400

    worker_id = datos.get("worker_id")
    url       = datos.get("url")

    if not worker_id or not url:
        return jsonify({"status": "error", "reason": "faltan worker_id o url"}), 400

    with _workers_lock:
        _workers[worker_id] = url
        if worker_id not in circuit_breakers:
            circuit_breakers[worker_id] = CircuitBreaker(
                name=worker_id,
                fail_max=config.FAIL_MAX,
                reset_timeout=config.RESET_TIMEOUT,
            )
        total = len(_workers)

    _save_registry()
    logger.info(f"Worker registrado   : {worker_id} @ {url}  (total: {total})")
    return jsonify({"status": "ok", "worker_id": worker_id, "total_workers": total})


@app.route("/workers/<worker_id>", methods=["DELETE"])
def deregister(worker_id: str):
    """
    Da de baja un worker del pool dinámico y actualiza el registro en disco.

    Útil para mantenimiento manual (apagar un worker para actualización, etc.)
    sin necesidad de reiniciar el Ambassador. El Circuit Breaker asociado
    también se elimina para liberar memoria.
    """
    with _workers_lock:
        if worker_id not in _workers:
            return jsonify({"status": "error", "reason": f"{worker_id} no está registrado"}), 404
        del _workers[worker_id]
        circuit_breakers.pop(worker_id, None)
        total = len(_workers)

    with _busy_lock:
        _busy_workers.discard(worker_id)

    _save_registry()
    logger.info(f"Worker eliminado    : {worker_id}  (total restante: {total})")
    return jsonify({"status": "ok", "worker_id": worker_id, "total_workers": total})


@app.route("/dispatch", methods=["POST"])
def dispatch():
    """
    Recibe un chunk del Coordinator y lo despacha a un worker disponible.

    Body esperado (JSON):
        chunk_id    (int)           : Identificador del fragmento.
        inicio      (int)           : Byte de inicio (inclusivo).
        fin         (int)           : Byte de fin (exclusivo).
        max_retries (int, opcional) : Reintentos máximos; usa config.MAX_RETRIES si se omite.
    """
    datos = request.get_json(silent=True)
    if not datos:
        return jsonify({
            "chunk_id": None, "worker_id": None,
            "status": "error", "reason": "cuerpo JSON inválido o ausente",
        }), 400

    chunk_id = datos.get("chunk_id")
    inicio   = datos.get("inicio")
    fin      = datos.get("fin")

    if chunk_id is None or inicio is None or fin is None:
        return jsonify({
            "chunk_id": chunk_id, "worker_id": None,
            "status": "error", "reason": "faltan campos: chunk_id, inicio, fin",
        }), 400

    if inicio > fin:
        return jsonify({
            "chunk_id": chunk_id, "worker_id": None,
            "status": "error", "reason": f"inicio ({inicio}) > fin ({fin})",
        }), 400

    max_retries = datos.get("max_retries", config.MAX_RETRIES)

    logger.info(f"Chunk recibido      : chunk_{chunk_id} (bytes {inicio} → {fin})")

    payload      = {"chunk_id": chunk_id, "inicio": inicio, "fin": fin}
    ya_fallaron: set[str] = set()

    for intento in range(1, max_retries + 1):

        worker_id = _seleccionar_worker(excluidos=ya_fallaron)

        if worker_id is None:
            logger.warning("Sin workers disponibles — abortando chunk")
            return jsonify({
                "chunk_id": chunk_id, "worker_id": None,
                "status": "error", "reason": "no_workers_available",
            }), 503

        cb = circuit_breakers[worker_id]
        logger.info(f"Worker seleccionado : {worker_id}")
        logger.info(f"Estado CB {worker_id:<12}: {cb.state.value}")
        logger.info(f"Intento             : {intento} / {max_retries}")

        try:
            t0        = time.monotonic()
            resultado = _despachar_a_worker(worker_id, payload)
            elapsed   = time.monotonic() - t0

            logger.info(f"Tiempo de respuesta : {elapsed:.3f}s")
            logger.info(f"Resultado           : OK")

            return jsonify({
                "chunk_id": chunk_id, "worker_id": worker_id,
                "status": "ok", "result": resultado,
            })

        except RuntimeError as error:
            logger.warning(f"Fallo en {worker_id}: {error}")
            ya_fallaron.add(worker_id)
            if intento < max_retries:
                logger.info(f"Reintentando : intento {intento + 1} / {max_retries}")

    logger.error(f"Max reintentos agotados para chunk_{chunk_id}")
    return jsonify({
        "chunk_id": chunk_id, "worker_id": None,
        "status": "error", "reason": "max_retries_exceeded",
    }), 502


@app.route("/workers/status", methods=["GET"])
def workers_status():
    """Estado del pool dinámico: workers registrados, activos, ocupados y CB."""
    with _workers_lock:
        registered = dict(_workers)

    with _busy_lock:
        busy = list(_busy_workers)

    detalle = {wid: circuit_breakers[wid].get_status() for wid in registered}
    activos  = [wid for wid, info in detalle.items() if info["state"] == "CLOSED"]

    return jsonify({
        "registered":   list(registered.keys()),
        "n_registered": len(registered),
        "activos":      activos,
        "n":            len(activos),
        "ocupados":     busy,
        "detalle":      detalle,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "ambassador"})


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _load_registry()
    logger.info("Iniciando Ambassador — esperando registros de workers")
    logger.info(
        f"FAIL_MAX={config.FAIL_MAX} | "
        f"RESET_TIMEOUT={config.RESET_TIMEOUT}s | "
        f"MAX_RETRIES={config.MAX_RETRIES}"
    )
    app.run(
        host=config.AMBASSADOR_HOST,
        port=config.AMBASSADOR_PORT,
        threaded=True,
    )
