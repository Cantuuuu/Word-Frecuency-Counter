"""
circuit_breaker.py — Circuit Breaker para el Ambassador.

Implementa una máquina de estados con tres estados para proteger
el acceso a cada worker ante fallos consecutivos:

  CLOSED    → operación normal, las peticiones pasan.
  OPEN      → fallo detectado, las peticiones se rechazan inmediatamente (fast-fail).
  HALF_OPEN → período de prueba, se permite UNA petición para verificar recuperación.

Interfaz expuesta:
  cb.allow_request()    → bool  (usado por el Ambassador en el round-robin)
  cb.record_success()   → None  (llamado tras una respuesta exitosa)
  cb.record_failure()   → None  (llamado tras un fallo)
  cb.call(fn, *a, **kw) → Any   (alternativa todo-en-uno; lanza CircuitBreakerOpen si OPEN)
  cb.get_status()       → dict  (para el endpoint /workers/status)
  cb.state              → State (propiedad de solo lectura)

Excepción:
  CircuitBreakerOpen — lanzada cuando se intenta usar call() con el CB en OPEN.

Transiciones de estado:
  CLOSED    → OPEN      cuando fail_count >= fail_max
  OPEN      → HALF_OPEN después de reset_timeout segundos
  HALF_OPEN → CLOSED    si la petición de prueba tiene éxito
  HALF_OPEN → OPEN      si la petición de prueba falla
"""

import threading
import time
from enum import Enum


# ---------------------------------------------------------------------------
# Estado del Circuit Breaker
# ---------------------------------------------------------------------------

class State(Enum):
    """Enum con los tres estados posibles del Circuit Breaker."""
    CLOSED    = "CLOSED"     # Operación normal
    OPEN      = "OPEN"       # Bloqueado por fallos
    HALF_OPEN = "HALF_OPEN"  # Probando recuperación


# ---------------------------------------------------------------------------
# Excepción personalizada
# ---------------------------------------------------------------------------

class CircuitBreakerOpen(Exception):
    """
    Lanzada cuando se intenta enviar una petición a través de un Circuit
    Breaker en estado OPEN. El Ambassador la captura para hacer fast-fail
    y rotar al siguiente worker.
    """
    pass


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    Circuit Breaker con máquina de estados completa y thread-safety garantizada.

    Usa threading.RLock para proteger el estado interno; el Ambassador corre
    con Flask threaded=True y múltiples hilos pueden llamar a este objeto
    de forma concurrente.

    Parámetros:
        name          (str): Identificador del worker asociado (ej. "worker_1").
        fail_max      (int): Fallos consecutivos necesarios para abrir el circuito.
        reset_timeout (int): Segundos en OPEN antes de pasar a HALF_OPEN.
    """

    def __init__(self, name: str, fail_max: int = 3, reset_timeout: int = 10):
        self.name          = name
        self.fail_max      = fail_max
        self.reset_timeout = reset_timeout

        # Estado interno — protegido por _lock
        self._state      = State.CLOSED
        self._fail_count = 0
        self._opened_at  = None   # time.monotonic() del último OPEN

        # RLock: un mismo hilo puede adquirirlo varias veces sin bloquearse
        self._lock = threading.RLock()

    # -----------------------------------------------------------------------
    # Estado actual (solo lectura)
    # -----------------------------------------------------------------------

    @property
    def state(self) -> State:
        """
        Estado actual del Circuit Breaker.

        Nota: si está en OPEN y ya expiró reset_timeout, esta propiedad NO
        transiciona a HALF_OPEN automáticamente — eso ocurre en allow_request().
        Úsala solo para logs o métricas.
        """
        with self._lock:
            return self._state

    # -----------------------------------------------------------------------
    # ¿Se permite esta petición?
    # -----------------------------------------------------------------------

    def allow_request(self) -> bool:
        """
        Decide si una petición puede enviarse al worker protegido.

          CLOSED    → True siempre.
          OPEN      → False, salvo que haya expirado reset_timeout; en ese caso
                      transiciona a HALF_OPEN y permite una sola petición de prueba.
          HALF_OPEN → True (petición de prueba ya en curso).
        """
        with self._lock:
            if self._state == State.CLOSED:
                return True

            if self._state == State.OPEN:
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.reset_timeout:
                    self._state = State.HALF_OPEN
                    return True
                return False

            # HALF_OPEN: permitir la petición de prueba
            return True

    # -----------------------------------------------------------------------
    # Registro de resultados
    # -----------------------------------------------------------------------

    def record_success(self) -> None:
        """
        Registra una petición exitosa.

          CLOSED    → reinicia fail_count.
          HALF_OPEN → transiciona a CLOSED (worker recuperado).
          OPEN      → no debería ocurrir; se ignora.
        """
        with self._lock:
            if self._state == State.HALF_OPEN:
                self._state      = State.CLOSED
                self._fail_count = 0
                self._opened_at  = None
            elif self._state == State.CLOSED:
                self._fail_count = 0

    def record_failure(self) -> None:
        """
        Registra una petición fallida (timeout, error HTTP, error en JSON).

          CLOSED    → incrementa fail_count; si >= fail_max, abre el circuito.
          HALF_OPEN → prueba fallida; vuelve a OPEN y reinicia el temporizador.
          OPEN      → actualiza opened_at para extender el timeout.
        """
        with self._lock:
            if self._state == State.CLOSED:
                self._fail_count += 1
                if self._fail_count >= self.fail_max:
                    self._state     = State.OPEN
                    self._opened_at = time.monotonic()

            elif self._state == State.HALF_OPEN:
                self._state     = State.OPEN
                self._opened_at = time.monotonic()

            elif self._state == State.OPEN:
                # Refrescar temporizador si otro hilo registra un fallo
                self._opened_at = time.monotonic()

    # -----------------------------------------------------------------------
    # Interfaz todo-en-uno
    # -----------------------------------------------------------------------

    def call(self, func, *args, **kwargs):
        """
        Ejecuta func(*args, **kwargs) si el CB lo permite; si no, lanza
        CircuitBreakerOpen. Registra éxito o fallo automáticamente.
        """
        if not self.allow_request():
            raise CircuitBreakerOpen(
                f"Circuit Breaker '{self.name}' está OPEN — petición rechazada."
            )
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    # -----------------------------------------------------------------------
    # Estado serializable para /workers/status
    # -----------------------------------------------------------------------

    def get_status(self) -> dict:
        """Retorna el estado del CB como dict apto para JSON."""
        with self._lock:
            segundos_abierto = None
            if self._state == State.OPEN and self._opened_at is not None:
                segundos_abierto = round(time.monotonic() - self._opened_at, 2)
            return {
                "name":         self.name,
                "state":        self._state.value,
                "fail_count":   self._fail_count,
                "seconds_open": segundos_abierto,
            }

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, "
            f"state={self._state.value}, "
            f"fail_count={self._fail_count}/{self.fail_max})"
        )
