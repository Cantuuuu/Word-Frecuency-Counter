"""
config.py — Configuración centralizada del sistema distribuido.

Todas las IPs, puertos y parámetros de ajuste están aquí.
Ningún otro archivo debe hardcodear valores; importa desde este módulo.

Las URLs de los workers se leen de variables de entorno para poder
ajustarlas en el archivo .env el día de la demo según la red WiFi del salón,
sin necesidad de tocar código fuente.
"""

import os

# ---------------------------------------------------------------------------
# Workers: nombre → URL base
# ---------------------------------------------------------------------------

WORKERS: dict[str, str] = {
    "worker_1": os.getenv("WORKER_1_URL", "http://192.168.1.101:5001"),
    "worker_2": os.getenv("WORKER_2_URL", "http://192.168.1.102:5001"),
    "worker_3": os.getenv("WORKER_3_URL", "http://192.168.1.103:5001"),
}

# ---------------------------------------------------------------------------
# Ambassador
# ---------------------------------------------------------------------------

AMBASSADOR_HOST = "0.0.0.0"   # Acepta conexiones desde cualquier IP de la red
AMBASSADOR_PORT = 5005

# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

FAIL_MAX      = 3    # Fallos consecutivos necesarios para abrir el circuito
RESET_TIMEOUT = 10   # Segundos en OPEN antes de pasar a HALF_OPEN

# ---------------------------------------------------------------------------
# Política de reintentos
# ---------------------------------------------------------------------------

MAX_RETRIES = 1   # 1 = sin reintentos. Con chunks de ~1.7 GB, un fallo es timeout
                  # genuino o worker caído; reintentar sobre el mismo archivo
                  # solo multiplica el tiempo de espera sin recuperar datos.

# ---------------------------------------------------------------------------
# Timeout de red
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT = 600  # 10 minutos — necesario para chunks de ~1.7 GB sobre
                       # disco local en Docker. Ajustar si los workers son más
                       # lentos (ej. HDD, red WiFi saturada).

# ---------------------------------------------------------------------------
# Archivo de texto
# ---------------------------------------------------------------------------

WIKI_PATH  = "/app/wiki_es.txt"   # Ruta dentro del contenedor Docker
N_PALABRAS = 3_000_000            # Palabras totales estimadas (para referencia)
