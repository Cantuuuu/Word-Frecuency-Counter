"""
ground_truth.py — Baseline secuencial para medir el tiempo de procesamiento.

Lee el archivo completo localmente usando un solo hilo y cuenta
las frecuencias. Este script sirve para:
1. Tener la "verdad absoluta" (ground truth) de los conteos.
2. Medir el tiempo que toma el procesamiento secuencial (T_seq).

En la demo, se comparará este tiempo con el tiempo del sistema distribuido
para calcular el Speedup y la Eficiencia.
"""

import time
import re
import os
from collections import Counter

# Mismo patrón exacto usado en los workers y el coordinator
PATRON_PALABRAS = re.compile(r"\b\w+\b")

# Ruta por defecto (la misma que usa el worker si se corre local)
WIKI_PATH = os.getenv("WIKI_PATH", "wiki_es.txt")

def main():
    if not os.path.exists(WIKI_PATH):
        print(f"❌ Error: No se encontró el archivo '{WIKI_PATH}'.")
        print("Asegúrate de que está en el directorio correcto o usa la variable WIKI_PATH.")
        return

    tamano_mb = os.path.getsize(WIKI_PATH) / (1024 * 1024)
    print(f"Iniciando conteo secuencial (Ground Truth)...")
    print(f"Archivo: {WIKI_PATH} ({tamano_mb:.2f} MB)")
    print("-" * 50)

    conteo_total = Counter()
    inicio = time.monotonic()

    # Leemos línea por línea para no cargar los 5GB en la memoria RAM.
    # errors="replace" para evitar crashes por caracteres malformados, igual que el worker.
    try:
        with open(WIKI_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # Normalizamos y extraemos palabras
                palabras = PATRON_PALABRAS.findall(line.lower())
                conteo_total.update(palabras)
    except KeyboardInterrupt:
        print("\n\n⚠️ Conteo cancelado por el usuario. Mostrando resultados parciales...")

    fin = time.monotonic()
    tiempo_total = fin - inicio

    print("-" * 50)
    print("✅ Conteo finalizado.")
    print(f"⏱️  Tiempo total    : {tiempo_total:.2f} segundos")
    print(f"📊 Palabras únicas : {len(conteo_total):,}")
    print(f"📈 Total procesadas: {sum(conteo_total.values()):,}")
    
    print("\n🏆 Top 10 palabras más frecuentes:")
    for palabra, frecuencia in conteo_total.most_common(10):
        print(f"  {palabra:<12} : {frecuencia:,}")

if __name__ == "__main__":
    main()
