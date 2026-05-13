"""
Circuit Breaker — protege a un worker contra fallos en cascada.

Un Circuit Breaker actúa como interruptor automático entre el Ambassador
y cada worker. En lugar de intentar una petición condenada al fracaso,
la bloquea de inmediato (fast-fail) cuando detecta fallos repetidos.

Estados:
  CLOSED    — operación normal; todas las peticiones pasan al worker.
  OPEN      — el worker falló demasiadas veces; peticiones bloqueadas sin llamar al worker.
  HALF_OPEN — modo prueba; se permite UNA petición para verificar si el worker se recuperó.

El Ambassador crea una instancia de CircuitBreaker por worker.
"""
import time


class CircuitBreakerOpen(Exception):
    """Se lanza cuando se intenta una petición y el CB está en estado OPEN."""
    pass


class CircuitBreaker:
    """
    Implementación del patrón Circuit Breaker para un worker individual.

    Parámetros
    ----------
    failure_threshold : int
        Número de fallos consecutivos que disparan la apertura del circuito (CLOSED → OPEN).
    recovery_timeout : float
        Segundos que el circuito permanece OPEN antes de pasar a HALF_OPEN y probar el worker.

    Consideraciones de concurrencia
    --------------------------------
    Esta implementación NO es thread-safe. Si el Ambassador despacha chunks
    en hilos paralelos, cada acceso a allow_request / record_success / record_failure
    debe protegerse con un threading.Lock externo.
    """

    def __init__(
        self,
        failure_threshold=3,
        recovery_timeout=10
    ):

        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self.failure_count = 0
        self.state = "CLOSED"
        self.last_failure_time = None  # marca de tiempo del último fallo; None si nunca ha fallado

    def allow_request(self):
        """
        Decide si se permite enviar una petición al worker.

        En CLOSED siempre retorna True.
        En OPEN verifica si ya expiró el recovery_timeout; si es así,
        transiciona a HALF_OPEN y retorna True para dejar pasar la prueba.
        En HALF_OPEN retorna True (la línea final): el estado ya se cambió
        en la llamada anterior y la prueba debe ejecutarse.

        Retorna
        -------
        bool
            True si la petición puede continuar.

        Lanza
        -----
        CircuitBreakerOpen
            Si el estado es OPEN y el timeout aún no expiró.
        """

        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":

            elapsed_time = (
                time.time() -
                self.last_failure_time
            )

            # Si pasó suficiente tiempo, damos una oportunidad de prueba al worker
            if elapsed_time >= self.recovery_timeout:
                self.state = "HALF_OPEN"
                return True

            # Timeout no expirado: rechazamos la petición sin tocar el worker
            raise CircuitBreakerOpen(
                "Circuit Breaker is OPEN"
            )

        # Aquí solo llega si el estado es HALF_OPEN (la prueba ya fue autorizada)
        return True

    def record_success(self):
        """
        Registra que la petición al worker fue exitosa.

        Si el CB estaba en HALF_OPEN, el éxito de la prueba cierra el circuito (HALF_OPEN → CLOSED).
        Reinicia el contador de fallos y borra la marca de tiempo del último fallo.
        """

        self.failure_count = 0
        self.state = "CLOSED"
        self.last_failure_time = None  # sin fallo activo, el timeout ya no tiene sentido

    def record_failure(self):
        """
        Registra que la petición al worker falló.

        Incrementa el contador y actualiza last_failure_time, que sirve como
        punto de partida para medir el recovery_timeout.

        Transiciones de estado:
          HALF_OPEN → OPEN : la prueba falló; el worker sigue caído.
          CLOSED    → OPEN : se alcanzó el umbral de fallos consecutivos.

        Parámetros: ninguno.
        Retorna: None.
        """

        self.failure_count += 1
        self.last_failure_time = time.time()  # referencia para calcular cuándo pasar a HALF_OPEN

        if self.state == "HALF_OPEN":
            # La prueba falló: volvemos a bloquear inmediatamente
            self.state = "OPEN"
        elif self.failure_count >= self.failure_threshold:
            # Umbral de fallos alcanzado: abrimos el circuito
            self.state = "OPEN"

    def get_state(self):
        """
        Retorna el estado actual del circuito como cadena de texto.

        Retorna
        -------
        str
            "CLOSED", "OPEN" o "HALF_OPEN".
        """
        return self.state
