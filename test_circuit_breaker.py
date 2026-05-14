"""
test_circuit_breaker.py — Tests unitarios del Circuit Breaker.

Cubre todas las transiciones de estado y la thread-safety básica.

Uso:
    python -m pytest test_circuit_breaker.py -v
    python test_circuit_breaker.py
"""

import threading
import time
import unittest

from circuit_breaker import CircuitBreaker, CircuitBreakerOpen, State


# ---------------------------------------------------------------------------
# Estado CLOSED
# ---------------------------------------------------------------------------

class TestClosed(unittest.TestCase):

    def setUp(self):
        self.cb = CircuitBreaker(name="test", fail_max=3, reset_timeout=10)

    def test_estado_inicial_es_closed(self):
        self.assertEqual(self.cb.state, State.CLOSED)

    def test_permite_peticion_en_closed(self):
        self.assertTrue(self.cb.allow_request())

    def test_exito_reinicia_fail_count(self):
        self.cb.record_failure()
        self.cb.record_failure()
        self.cb.record_success()
        self.assertEqual(self.cb._fail_count, 0)
        self.assertEqual(self.cb.state, State.CLOSED)

    def test_no_abre_antes_de_fail_max(self):
        for _ in range(2):
            self.cb.record_failure()
        self.assertEqual(self.cb.state, State.CLOSED)

    def test_abre_al_alcanzar_fail_max(self):
        for _ in range(3):
            self.cb.record_failure()
        self.assertEqual(self.cb.state, State.OPEN)


# ---------------------------------------------------------------------------
# Estado OPEN
# ---------------------------------------------------------------------------

class TestOpen(unittest.TestCase):

    def setUp(self):
        # fail_max=1 para abrir con un solo fallo
        self.cb = CircuitBreaker(name="test", fail_max=1, reset_timeout=1)
        self.cb.record_failure()

    def test_estado_es_open(self):
        self.assertEqual(self.cb.state, State.OPEN)

    def test_bloquea_peticion_en_open(self):
        self.assertFalse(self.cb.allow_request())

    def test_transiciona_a_half_open_tras_timeout(self):
        time.sleep(1.1)
        resultado = self.cb.allow_request()
        self.assertTrue(resultado)
        self.assertEqual(self.cb.state, State.HALF_OPEN)

    def test_no_transiciona_antes_de_timeout(self):
        # Verificar inmediatamente después de abrir
        self.assertFalse(self.cb.allow_request())
        self.assertEqual(self.cb.state, State.OPEN)

    def test_fallo_en_open_actualiza_opened_at(self):
        t_antes = self.cb._opened_at
        time.sleep(0.05)
        self.cb.record_failure()
        self.assertGreater(self.cb._opened_at, t_antes)


# ---------------------------------------------------------------------------
# Estado HALF_OPEN
# ---------------------------------------------------------------------------

class TestHalfOpen(unittest.TestCase):

    def setUp(self):
        # reset_timeout=0 para transicionar inmediatamente a HALF_OPEN
        self.cb = CircuitBreaker(name="test", fail_max=1, reset_timeout=0)
        self.cb.record_failure()   # → OPEN
        self.cb.allow_request()    # → HALF_OPEN (timeout=0 ya expiró)

    def test_estado_es_half_open(self):
        self.assertEqual(self.cb.state, State.HALF_OPEN)

    def test_exito_cierra_circuito(self):
        self.cb.record_success()
        self.assertEqual(self.cb.state, State.CLOSED)
        self.assertEqual(self.cb._fail_count, 0)

    def test_fallo_reabre_circuito(self):
        self.cb.record_failure()
        self.assertEqual(self.cb.state, State.OPEN)

    def test_permite_peticion_en_half_open(self):
        self.assertTrue(self.cb.allow_request())


# ---------------------------------------------------------------------------
# Interfaz call()
# ---------------------------------------------------------------------------

class TestCall(unittest.TestCase):

    def setUp(self):
        self.cb = CircuitBreaker(name="test", fail_max=2, reset_timeout=10)

    def test_call_ejecuta_funcion(self):
        resultado = self.cb.call(lambda: 42)
        self.assertEqual(resultado, 42)

    def test_call_lanza_circuit_breaker_open_si_open(self):
        self.cb.record_failure()
        self.cb.record_failure()   # fail_max=2 → OPEN
        with self.assertRaises(CircuitBreakerOpen):
            self.cb.call(lambda: None)

    def test_call_registra_fallo_ante_excepcion(self):
        def lanzar():
            raise ValueError("error de prueba")

        with self.assertRaises(ValueError):
            self.cb.call(lanzar)

        self.assertEqual(self.cb._fail_count, 1)

    def test_call_registra_exito(self):
        self.cb.record_failure()   # fail_count=1
        self.cb.call(lambda: None)
        self.assertEqual(self.cb._fail_count, 0)


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------

class TestGetStatus(unittest.TestCase):

    def test_status_en_closed(self):
        cb = CircuitBreaker(name="w1", fail_max=3, reset_timeout=10)
        s = cb.get_status()
        self.assertEqual(s["state"], "CLOSED")
        self.assertEqual(s["fail_count"], 0)
        self.assertIsNone(s["seconds_open"])

    def test_status_en_open(self):
        cb = CircuitBreaker(name="w1", fail_max=1, reset_timeout=10)
        cb.record_failure()
        s = cb.get_status()
        self.assertEqual(s["state"], "OPEN")
        self.assertIsNotNone(s["seconds_open"])
        self.assertGreaterEqual(s["seconds_open"], 0)

    def test_status_contiene_name(self):
        cb = CircuitBreaker(name="worker_2", fail_max=3, reset_timeout=10)
        self.assertEqual(cb.get_status()["name"], "worker_2")


# ---------------------------------------------------------------------------
# Thread-safety básica
# ---------------------------------------------------------------------------

class TestThreadSafety(unittest.TestCase):

    def test_fallos_concurrentes_no_superan_fail_max(self):
        """Múltiples hilos registrando fallos no deben corromper el estado."""
        cb = CircuitBreaker(name="concurrent", fail_max=5, reset_timeout=60)
        errores = []

        def fallar():
            try:
                cb.record_failure()
            except Exception as e:
                errores.append(e)

        hilos = [threading.Thread(target=fallar) for _ in range(20)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        self.assertEqual(len(errores), 0)
        # El estado debe ser OPEN o CLOSED, nunca inválido
        self.assertIn(cb.state, (State.CLOSED, State.OPEN))

    def test_allow_request_es_thread_safe(self):
        """allow_request() desde múltiples hilos no lanza excepciones."""
        cb = CircuitBreaker(name="concurrent", fail_max=3, reset_timeout=60)
        errores = []

        def verificar():
            try:
                cb.allow_request()
            except Exception as e:
                errores.append(e)

        hilos = [threading.Thread(target=verificar) for _ in range(50)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        self.assertEqual(len(errores), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
