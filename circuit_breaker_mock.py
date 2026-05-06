"""
circuit_breaker_mock.py — Mock del Circuit Breaker para pruebas del Ambassador.

Emula el comportamiento completo de un Circuit Breaker real con tres estados:
  CLOSED   → operación normal, las peticiones pasan.
  OPEN     → fallo detectado, las peticiones se rechazan inmediatamente (fast-fail).
  HALF_OPEN → período de prueba, se permite UNA petición para saber si el worker se recuperó.

Interfaz expuesta (idéntica a circuit_breaker.py del compañero):
  cb.allow_request()    → bool  (usado directamente por el Ambassador en el round-robin)
  cb.record_success()   → None  (llama el Ambassador tras una respuesta exitosa)
  cb.record_failure()   → None  (llama el Ambassador tras un fallo)
  cb.call(fn, *a, **kw) → Any   (alternativa todo-en-uno; levanta CircuitBreakerOpen si OPEN)
  cb.get_status()       → dict  (para el endpoint /workers/status)
  cb.state              → State (propiedad de solo lectura)

Excepción:
  CircuitBreakerOpen — levantada cuando se intenta llamar a call() con el CB en OPEN.

Transiciones de estado:
  CLOSED   → OPEN      cuando fail_count >= fail_max
  OPEN     → HALF_OPEN después de reset_timeout segundos
  HALF_OPEN → CLOSED   si la petición de prueba tiene éxito
  HALF_OPEN → OPEN     si la petición de prueba falla
"""

import time
import threading
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
    Excepción que se lanza cuando se intenta enviar una petición
    a través de un Circuit Breaker que está en estado OPEN.

    El Ambassador la captura para hacer fast-fail y rotar al siguiente worker.
    """
    pass


# ---------------------------------------------------------------------------
# Circuit Breaker Mock
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    Implementación mock de un Circuit Breaker con máquina de estados completa.

    Es thread-safe: usa threading.Lock para proteger todas las lecturas y
    escrituras sobre el estado interno, ya que el Ambassador corre con
    Flask threaded=True y múltiples hilos pueden llamar a este objeto
    de forma concurrente.

    Parámetros:
        name          (str): Identificador del worker asociado (ej. "worker_1").
        fail_max      (int): Número de fallos consecutivos que abren el circuito.
        reset_timeout (int): Segundos que el CB permanece en OPEN antes de pasar a HALF_OPEN.
    """

    def __init__(self, name: str, fail_max: int = 3, reset_timeout: int = 10):
        """
        Inicializa el Circuit Breaker en estado CLOSED con contadores en cero.

        Parámetros:
            name          (str): Nombre del worker que protege este CB.
            fail_max      (int): Umbral de fallos para abrir el circuito.
            reset_timeout (int): Segundos de espera antes de intentar HALF_OPEN.
        """
        self.name          = name
        self.fail_max      = fail_max
        self.reset_timeout = reset_timeout

        # Estado interno — protegido por _lock
        self._state      = State.CLOSED
        self._fail_count = 0           # Fallos consecutivos en estado CLOSED
        self._opened_at  = None        # Timestamp (time.monotonic) del último OPEN

        # Lock de reentrada para que un mismo hilo no se bloquee a sí mismo
        self._lock = threading.RLock()

    # -----------------------------------------------------------------------
    # Propiedad de solo lectura: estado actual
    # -----------------------------------------------------------------------

    @property
    def state(self) -> State:
        """
        Retorna el estado actual del Circuit Breaker.

        Nota: si el CB está en OPEN y ya pasó reset_timeout, esta propiedad
        NO transiciona a HALF_OPEN automáticamente — eso ocurre en allow_request().
        Usar solo para lecturas de estado en logs o métricas.

        Retorna:
            State: Estado actual (CLOSED, OPEN o HALF_OPEN).
        """
        with self._lock:
            return self._state

    # -----------------------------------------------------------------------
    # Método principal: ¿se permite esta petición?
    # -----------------------------------------------------------------------

    def allow_request(self) -> bool:
        """
        Decide si se debe enviar una petición al worker protegido por este CB.

        Lógica:
          - CLOSED   → siempre permite (retorna True).
          - OPEN     → bloquea (retorna False), SALVO que haya expirado
                       reset_timeout; en ese caso transiciona a HALF_OPEN
                       y permite UNA petición de prueba.
          - HALF_OPEN → permite exactamente una petición de prueba (retorna True).
                        Peticiones adicionales concurrentes son bloqueadas
                        hasta que la prueba resuelva.

        Retorna:
            bool: True si la petición puede enviarse, False si debe rechazarse.
        """
        with self._lock:
            if self._state == State.CLOSED:
                return True

            if self._state == State.OPEN:
                # Verificar si ya pasó el tiempo de espera
                tiempo_transcurrido = time.monotonic() - self._opened_at
                if tiempo_transcurrido >= self.reset_timeout:
                    # Transición OPEN → HALF_OPEN: permitir una sola petición de prueba
                    self._state = State.HALF_OPEN
                    return True
                # Aún en OPEN: rechazar sin intentar
                return False

            # Estado HALF_OPEN: permitir la petición de prueba
            if self._state == State.HALF_OPEN:
                return True

    # -----------------------------------------------------------------------
    # Registro de resultados
    # -----------------------------------------------------------------------

    def record_success(self) -> None:
        """
        Registra que la petición al worker fue exitosa.

        Transiciones posibles:
          - CLOSED   → permanece CLOSED, reinicia el contador de fallos.
          - HALF_OPEN → transiciona a CLOSED (el worker se recuperó).
          - OPEN     → no debería ocurrir, pero si ocurre, se ignora.
        """
        with self._lock:
            if self._state == State.HALF_OPEN:
                # Prueba exitosa: el worker está sano de nuevo
                self._state      = State.CLOSED
                self._fail_count = 0
                self._opened_at  = None
            elif self._state == State.CLOSED:
                # Reiniciar contador tras una respuesta buena
                self._fail_count = 0

    def record_failure(self) -> None:
        """
        Registra que la petición al worker falló (timeout, error HTTP, error en JSON).

        Transiciones posibles:
          - CLOSED   → incrementa fail_count; si alcanza fail_max, abre el circuito (→ OPEN).
          - HALF_OPEN → la prueba falló; vuelve a OPEN y reinicia el temporizador.
          - OPEN     → ya estaba abierto; se actualiza opened_at para extender el timeout.
        """
        with self._lock:
            if self._state == State.CLOSED:
                self._fail_count += 1
                if self._fail_count >= self.fail_max:
                    # Umbral superado: abrir el circuito
                    self._state     = State.OPEN
                    self._opened_at = time.monotonic()

            elif self._state == State.HALF_OPEN:
                # La prueba falló: volver a OPEN y reiniciar el temporizador
                self._state     = State.OPEN
                self._opened_at = time.monotonic()

            elif self._state == State.OPEN:
                # Refrescar el temporizador (ej. si el Ambassador llama desde otro hilo)
                self._opened_at = time.monotonic()

    # -----------------------------------------------------------------------
    # Interfaz alternativa: call()
    # -----------------------------------------------------------------------

    def call(self, func, *args, **kwargs):
        """
        Ejecuta func(*args, **kwargs) si el CB lo permite; si no, lanza CircuitBreakerOpen.

        Esta interfaz encapsula allow_request + record_success/failure en un único
        punto de llamada. Es la alternativa a usar allow_request/record_* directamente.

        Parámetros:
            func     (callable): Función a ejecutar (normalmente una llamada HTTP con requests).
            *args               : Argumentos posicionales para func.
            **kwargs            : Argumentos de palabra clave para func.

        Retorna:
            Any: El valor retornado por func si tiene éxito.

        Levanta:
            CircuitBreakerOpen : Si el CB está en OPEN y no ha expirado reset_timeout.
            Exception          : Cualquier excepción que lance func (después de registrarla).
        """
        if not self.allow_request():
            raise CircuitBreakerOpen(
                f"Circuit Breaker '{self.name}' está OPEN — petición rechazada."
            )

        try:
            resultado = func(*args, **kwargs)
            self.record_success()
            return resultado
        except Exception:
            self.record_failure()
            raise  # Re-lanzar para que el Ambassador la maneje

    # -----------------------------------------------------------------------
    # Estado serializable para el endpoint /workers/status
    # -----------------------------------------------------------------------

    def get_status(self) -> dict:
        """
        Retorna un diccionario con el estado actual del CB, apto para serializar a JSON.

        Retorna:
            dict con las claves:
              - "name"       (str): Identificador del worker.
              - "state"      (str): Estado actual ("CLOSED", "OPEN" o "HALF_OPEN").
              - "fail_count" (int): Fallos consecutivos actuales.
              - "seconds_open" (float | None): Segundos que lleva en OPEN, o None.
        """
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

    # -----------------------------------------------------------------------
    # Representación para logs y depuración
    # -----------------------------------------------------------------------

    def __repr__(self) -> str:
        """Representación legible del CB — útil al imprimir en logs."""
        return (
            f"CircuitBreaker(name={self.name!r}, "
            f"state={self._state.value}, "
            f"fail_count={self._fail_count}/{self.fail_max})"
        )
