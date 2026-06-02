"""
run_fault_test.py — Ejecuta un CASO DE PRUEBA CON FALLOS contra el Coordinator
y registra el resultado en resultados/tolerancia_fallos.csv.

Los tres casos obligatorios del proyecto:
  1. 1 worker cae a mitad del procesamiento del corpus de 1 GB.
  2. 2 workers caen simultáneamente durante el procesamiento de 3 GB.
  3. 1 worker cae y luego se recupera (OPEN → HALF_OPEN → CLOSED).

Este script NO puede tumbar los workers de tus compañeros por ti: el fallo se
induce MANUALMENTE durante la corrida. Formas de inducir un fallo:
  - El dueño del worker hace  `docker stop <contenedor>`  (o Ctrl-C en su terminal).
  - O levantar un worker con  -e FAIL_MODE=true   → responde HTTP 500 siempre.
  - O con  -e DELAY=<segundos>  → simula un worker lento que dispara el timeout.
  Para el caso 3 (recuperación), se vuelve a levantar el worker y se auto-registra;
  el Circuit Breaker pasará OPEN → HALF_OPEN → CLOSED solo.

Flujo recomendado:
  1. Levanta todos los workers y verifica  /workers/status.
  2. Corre este script con el caso y las notas:
        python run_fault_test.py --caso 1 --corpus 1GB --workers-caidos 1 \
            --notas "worker_03 detenido al ~50%"
  3. Cuando el script imprima 'estado → running', INDUCE el fallo (paso manual).
  4. El script espera a que el sistema redistribuya y complete; guarda la fila.

Mide y guarda: tiempo total, reintentos (cuántas veces se reasignó carga),
correctitud contra el ground truth, y tus notas de cómo se indujo el fallo.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

import requests

RAIZ_REPO  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_RESULT = os.path.join(RAIZ_REPO, "resultados")
CSV_PATH   = os.path.join(DIR_RESULT, "tolerancia_fallos.csv")

COLUMNAS = [
    "timestamp",
    "caso",
    "corpus",
    "n_workers_inicial",
    "n_workers_caidos",
    "t_distribuido_s",
    "reintentos",
    "correcto",
    "palabras_unicas",
    "notas",
]

POLL_INTERVAL = 5
TIMEOUT_TOTAL = 7200


def _workers_registrados(ambassador: str) -> int:
    try:
        data = requests.get(f"{ambassador}/workers/status", timeout=10).json()
        return data.get("n_registered", len(data.get("registered", [])))
    except requests.RequestException:
        return -1


def _esperar_done(coordinator: str) -> dict:
    t0 = time.monotonic()
    ultimo = None
    induccion_avisada = False
    while True:
        if time.monotonic() - t0 > TIMEOUT_TOTAL:
            sys.exit("✗ Timeout: el caso superó el tope de seguridad.")
        try:
            estado = requests.get(f"{coordinator}/status", timeout=10).json().get("estado")
        except requests.RequestException:
            time.sleep(POLL_INTERVAL)
            continue

        if estado != ultimo:
            print(f"  estado → {estado}")
            ultimo = estado
            if estado == "running" and not induccion_avisada:
                print("  >>> AHORA induce el/los fallo(s) manualmente (detén worker(s)). <<<")
                induccion_avisada = True

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
    p = argparse.ArgumentParser(description="Corre un caso de prueba con fallos y lo guarda en CSV.")
    p.add_argument("--coordinator", default=os.getenv("COORDINATOR_URL", "http://localhost:4999"))
    p.add_argument("--ambassador",  default=os.getenv("AMBASSADOR_URL", "http://localhost:5005"))
    p.add_argument("--caso", required=True, help="Identificador del caso (ej. '1', '2', '3-recuperacion').")
    p.add_argument("--corpus", required=True, help="Etiqueta del corpus usado (ej. '1GB', '3GB').")
    p.add_argument("--corpus-gb", type=float, default=None,
                   help="GB del corpus a procesar (limita el archivo). Si se omite, archivo completo.")
    p.add_argument("--workers-caidos", type=int, default=0,
                   help="Cuántos workers se tumbaron en este caso.")
    p.add_argument("--notas", default="", help="Cómo y cuándo se indujo el fallo.")
    args = p.parse_args()

    coordinator = args.coordinator.rstrip("/")
    ambassador  = args.ambassador.rstrip("/")

    # Reset previo
    try:
        requests.post(f"{coordinator}/reset", timeout=30)
        requests.post(f"{ambassador}/workers/reset-cbs", timeout=30)
    except requests.RequestException as e:
        print(f"  (reset previo omitido: {e})")

    n_inicial = _workers_registrados(ambassador)
    print(f"Workers registrados antes de arrancar: {n_inicial}")

    cuerpo: dict = {"ground_truth": True}
    if args.corpus_gb is not None:
        cuerpo["corpus_gb"] = args.corpus_gb
    print(f"→ Arrancando procesamiento (ground_truth=true, corpus_gb={args.corpus_gb}) ...")
    try:
        resp = requests.post(f"{coordinator}/start", json=cuerpo, timeout=30)
        resp.raise_for_status()
    except requests.HTTPError as e:
        sys.exit(f"✗ No se pudo arrancar: {e} — {e.response.text if e.response else ''}")

    res = _esperar_done(coordinator)

    fila = {
        "timestamp":          datetime.now().isoformat(timespec="seconds"),
        "caso":               args.caso,
        "corpus":             args.corpus,
        "n_workers_inicial":  n_inicial,
        "n_workers_caidos":   args.workers_caidos,
        "t_distribuido_s":    res.get("tiempo_distribuido"),
        "reintentos":         res.get("reintentos_totales"),
        "correcto":           res.get("correcto"),
        "palabras_unicas":    res.get("palabras_unicas"),
        "notas":              args.notas,
    }
    _guardar_fila(fila)

    print("\n" + "=" * 60)
    print(f"  Caso              : {args.caso}  (corpus {args.corpus})")
    print(f"  Workers inicial   : {n_inicial}   caídos: {args.workers_caidos}")
    print(f"  T. distribuido    : {fila['t_distribuido_s']} s")
    print(f"  Reintentos        : {fila['reintentos']}  (reasignaciones de carga)")
    print(f"  Correctitud       : {'✓' if fila['correcto'] else '✗'}  (sin perder fragmentos)")
    print("=" * 60)
    print(f"✓ Fila agregada a {CSV_PATH}")


if __name__ == "__main__":
    main()
