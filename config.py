"""
config.py — Configuración centralizada del sistema distribuido.

Todas las IPs, puertos y parámetros de ajuste están aquí.
Ningún otro archivo debe hardcodear valores; importa desde este módulo.

Las URLs de los workers se leen de variables de entorno para poder
ajustarlas en el archivo .env el día de la demo según la red WiFi del salón,
sin necesidad de tocar código fuente.
"""

# ---------------------------------------------------------------------------
# Ambassador
# ---------------------------------------------------------------------------

AMBASSADOR_HOST = "0.0.0.0"   # Acepta conexiones desde cualquier IP de la red
AMBASSADOR_PORT = 5005

# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

FAIL_MAX      = 2    # Fallos consecutivos para abrir el circuito (2 = reacciona rápido)
RESET_TIMEOUT = 10   # Segundos en OPEN antes de pasar a HALF_OPEN

# ---------------------------------------------------------------------------
# Política de reintentos
# ---------------------------------------------------------------------------

MAX_RETRIES = 2   # Intentos por chunk en el Ambassador (2 = 1 reintento real).
                  # El coordinator también hace un pase de auto-reintento al final
                  # para chunks que fallaron en todos los intentos del Ambassador.

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

# ---------------------------------------------------------------------------
# Worker — lectura de archivos grandes
# ---------------------------------------------------------------------------

BUFFER_SIZE  = 64 * 1024 * 1024  # 64 MB por bloque de lectura
BOUNDARY_BUF = 512                # Bytes extra para capturar palabras en el límite de chunk
