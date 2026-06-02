"""
graficar.py — Genera las gráficas del informe a partir de resultados/experimentos.csv.

Produce dos PNG en resultados/:
  - tiempos.png : barras de T_secuencial vs T_distribuido por tamaño de corpus.
  - speedup.png : línea de speedup vs tamaño de corpus (con referencia y=1).

Requiere matplotlib (no es dependencia del sistema distribuido, solo del informe):
    pip install matplotlib

Uso:
    python graficar.py

Si hay varias corridas con la misma etiqueta, usa la última (la más reciente).
"""

import csv
import os
import sys

RAIZ_REPO  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_RESULT = os.path.join(RAIZ_REPO, "resultados")
CSV_PATH   = os.path.join(DIR_RESULT, "experimentos.csv")


def _cargar() -> list[dict]:
    if not os.path.exists(CSV_PATH):
        sys.exit(f"✗ No existe {CSV_PATH}. Corre primero run_experiment.py.")
    with open(CSV_PATH, encoding="utf-8") as f:
        filas = list(csv.DictReader(f))
    if not filas:
        sys.exit("✗ El CSV está vacío.")
    # Última corrida por etiqueta (las filas se agregan en orden cronológico)
    por_etiqueta: dict[str, dict] = {}
    for fila in filas:
        por_etiqueta[fila["etiqueta"]] = fila
    # Ordenar por corpus_gb numérico cuando sea posible
    def _clave(fila):
        try:
            return float(fila.get("corpus_gb") or 0)
        except ValueError:
            return 0.0
    return sorted(por_etiqueta.values(), key=_clave)


def main() -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("✗ Falta matplotlib. Instala con:  pip install matplotlib")

    filas = _cargar()
    etiquetas = [f["etiqueta"] for f in filas]

    def _num(filas, col):
        out = []
        for f in filas:
            v = f.get(col)
            out.append(float(v) if v not in (None, "", "None") else None)
        return out

    t_seq   = _num(filas, "t_secuencial_s")
    t_dist  = _num(filas, "t_distribuido_s")
    speedup = _num(filas, "speedup")

    os.makedirs(DIR_RESULT, exist_ok=True)

    # --- Gráfica 1: tiempos seq vs dist ---
    x = range(len(etiquetas))
    ancho = 0.35
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar([i - ancho / 2 for i in x], [v or 0 for v in t_seq],  ancho, label="Secuencial")
    ax.bar([i + ancho / 2 for i in x], [v or 0 for v in t_dist], ancho, label="Distribuido")
    ax.set_xticks(list(x))
    ax.set_xticklabels(etiquetas)
    ax.set_ylabel("Tiempo (s)")
    ax.set_title("Tiempo secuencial vs distribuido por tamaño de corpus")
    ax.legend()
    fig.tight_layout()
    out1 = os.path.join(DIR_RESULT, "tiempos.png")
    fig.savefig(out1, dpi=120)
    print(f"✓ {out1}")

    # --- Gráfica 2: speedup ---
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(etiquetas, [v or 0 for v in speedup], marker="o", label="Speedup")
    ax.axhline(1.0, linestyle="--", color="gray", label="Sin mejora (y=1)")
    ax.set_ylabel("Speedup (T_seq / T_dist)")
    ax.set_title("Speedup por tamaño de corpus")
    ax.legend()
    fig.tight_layout()
    out2 = os.path.join(DIR_RESULT, "speedup.png")
    fig.savefig(out2, dpi=120)
    print(f"✓ {out2}")


if __name__ == "__main__":
    main()
