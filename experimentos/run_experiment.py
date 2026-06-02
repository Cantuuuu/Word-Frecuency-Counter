"""
run_experiment.py — Ejecuta UN experimento de rendimiento contra el Coordinator
y registra el resultado en resultados/experimentos.csv.

Pensado para la demo en red: tú levantas Coordinator + Ambassador en tu PC, tus
compañeros levantan sus workers (cada uno con su copia local del corpus). Este
script NO levanta workers — solo orquesta una corrida vía la API HTTP del
Coordinator y guarda los números.

El tamaño del corpus se controla desde aquí con --corpus-gb: el coordinator solo
procesa los primeros N GB del archivo (un único archivo de 5 GB sirve para los 5
experimentos, no hace falta generar archivos separados). El archivo debe ser el
mismo byte a byte en todos los nodos.

Antes de correrlo, asegúrate de que:
  - El mismo archivo está cargado en TODOS los nodos (workers y coordinator).
  - Todos los workers aparecen en  GET http://<tu-pc>:5005/workers/status

Uso:
    python run_experiment.py --corpus-gb 1                 # procesa 1 GB
    python run_experiment.py --corpus-gb 3 --etiqueta 3GB
    python run_experiment.py                                # archivo completo
    python run_experiment.py --corpus-gb 1 --coordinator http://192.168.1.50:4999

Qué hace:
  1. POST /reset             → deja el coordinator en idle (si venía de una corrida)
  2. POST /workers/reset-cbs → limpia Circuit Breakers de corridas previas
  3. POST /start             → arranca con ground_truth=true y corpus_gb (seq vs dist + correctitud)
  4. Polla GET /status       → hasta que el estado sea 'done' o 'error'
  5. GET /result             → toma los números finales
  6. Agrega una fila a resultados/experimentos.csv

El conteo secuencial (ground truth) puede tardar varios minutos en corpus de
varios GB: es parte del experimento (es el baseline T_secuencial).
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

import requests

# Carpeta de resultados en la raíz del repo (un nivel arriba de experimentos/)
RAIZ_REPO   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_RESULT  = os.path.join(RAIZ_REPO, "resultados")
CSV_PATH    = os.path.join(DIR_RESULT, "experimentos.csv")

COLUMNAS = [
    "timestamp",
    "etiqueta",
    "corpus_gb",
    "archivo_bytes",
    "n_workers",
    "t_secuencial_s",
    "t_distribuido_s",
    "speedup",
    "palabras_unicas",
    "total_palabras_dist",
    "total_palabras_seq",
    "correcto",
    "reintentos",
]

POLL_INTERVAL = 5     # Segundos entre cada consulta a /status
TIMEOUT_TOTAL = 7200  # Tope de seguridad: 2 h por experimento


def _post(url: str, **kwargs) -> requests.Response:
    r = requests.post(url, timeout=30, **kwargs)
    r.raise_for_status()
    return r


def _esperar_done(coordinator: str) -> dict:
    """Polla /status hasta done/error y devuelve el bloque result de /result."""
    t0 = time.monotonic()
    ultimo_estado = None
    while True:
        if time.monotonic() - t0 > TIMEOUT_TOTAL:
            sys.exit("✗ Timeout: el experimento superó el tope de seguridad.")

        try:
            estado = requests.get(f"{coordinator}/status", timeout=10).json().get("estado")
        except requests.RequestException as e:
            print(f"  (status no disponible: {e} — reintentando)")
            time.sleep(POLL_INTERVAL)
            continue

        if estado != ultimo_estado:
            print(f"  estado → {estado}")
            ultimo_estado = estado

        if estado == "done":
            return requests.get(f"{coordinator}/result", timeout=10).json().get("result", {})
        if estado == "error":
            res = requests.get(f"{coordinator}/result", timeout=10).json().get("result", {})
            sys.exit(f"✗ El coordinator reportó error: {res.get('reason', 'desconocido')}")

        time.sleep(POLL_INTERVAL)


def _guardar_fila(fila: dict) -> None:
    os.makedirs(DIR_RESULT, exist_ok=True)
    nuevo = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        if nuevo:
            w.writeheader()
        w.writerow(fila)


def main() -> None:
    p = argparse.ArgumentParser(description="Corre un experimento de rendimiento y lo guarda en CSV.")
    p.add_argument("--coordinator", default=os.getenv("COORDINATOR_URL", "http://localhost:4999"),
                   help="URL del Coordinator (default: http://localhost:4999)")
    p.add_argument("--ambassador", default=os.getenv("AMBASSADOR_URL", "http://localhost:5005"),
                   help="URL del Ambassador (default: http://localhost:5005)")
    p.add_argument("--corpus-gb", type=float, default=None,
                   help="GB del corpus a procesar (1, 2, 3, 4, 5). Si se omite, "
                        "se procesa el archivo completo.")
    p.add_argument("--etiqueta", default="",
                   help="Etiqueta libre para la corrida (ej. '1GB', '3GB-corrida2'). "
                        "Si se omite, se deriva del tamaño procesado.")
    args = p.parse_args()

    coordinator = args.coordinator.rstrip("/")
    ambassador  = args.ambassador.rstrip("/")

    print(f"Coordinator : {coordinator}")
    print(f"Ambassador  : {ambassador}")

    # 1. Reset del coordinator (ignora 409 si ya estaba idle/running ajeno)
    try:
        _post(f"{coordinator}/reset")
        print("✓ Coordinator reseteado a idle")
    except requests.HTTPError as e:
        print(f"  (reset omitido: {e})")

    # 2. Reset de Circuit Breakers
    try:
        _post(f"{ambassador}/workers/reset-cbs")
        print("✓ Circuit Breakers reseteados")
    except requests.RequestException as e:
        print(f"  (reset-cbs omitido: {e})")

    # 3. Arrancar con ground truth (necesario para speedup y correctitud)
    cuerpo: dict = {"ground_truth": True}
    if args.corpus_gb is not None:
        cuerpo["corpus_gb"] = args.corpus_gb
    print(f"→ Arrancando procesamiento (ground_truth=true, corpus_gb={args.corpus_gb}) ...")
    try:
        resp = _post(f"{coordinator}/start", json=cuerpo).json()
    except requests.HTTPError as e:
        sys.exit(f"✗ No se pudo arrancar: {e} — {e.response.text if e.response else ''}")
    print(f"  workers: {resp.get('workers')}  (n_chunks={resp.get('num_chunks')})")

    # 4-5. Esperar y recoger resultado
    res = _esperar_done(coordinator)

    bytes_archivo = res.get("archivo_bytes")
    corpus_gb     = round(bytes_archivo / 1024**3, 2) if bytes_archivo else None
    etiqueta      = args.etiqueta or (f"{corpus_gb}GB" if corpus_gb else "sin_etiqueta")

    fila = {
        "timestamp":            datetime.now().isoformat(timespec="seconds"),
        "etiqueta":             etiqueta,
        "corpus_gb":            corpus_gb,
        "archivo_bytes":        bytes_archivo,
        "n_workers":            res.get("num_chunks"),
        "t_secuencial_s":       res.get("tiempo_secuencial"),
        "t_distribuido_s":      res.get("tiempo_distribuido"),
        "speedup":              res.get("speedup"),
        "palabras_unicas":      res.get("palabras_unicas"),
        "total_palabras_dist":  res.get("total_palabras_distribuido"),
        "total_palabras_seq":   res.get("total_palabras_secuencial"),
        "correcto":             res.get("correcto"),
        "reintentos":           res.get("reintentos_totales"),
    }

    _guardar_fila(fila)

    print("\n" + "=" * 60)
    print(f"  Corpus            : {etiqueta}  ({bytes_archivo:,} bytes)" if bytes_archivo else f"  Corpus: {etiqueta}")
    print(f"  T. secuencial     : {fila['t_secuencial_s']} s")
    print(f"  T. distribuido    : {fila['t_distribuido_s']} s")
    print(f"  Speedup           : {fila['speedup']}x")
    print(f"  Correctitud       : {'✓' if fila['correcto'] else '✗'}  (dist == ground truth)")
    print(f"  Reintentos        : {fila['reintentos']}")
    print("=" * 60)
    print(f"✓ Fila agregada a {CSV_PATH}")


if __name__ == "__main__":
    main()
