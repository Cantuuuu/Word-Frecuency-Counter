# Experimentos

Scripts para correr los experimentos del proyecto y guardar los resultados en CSV.
No levantan workers: orquestan corridas vía la API HTTP del Coordinator. Tú levantas
Coordinator + Ambassador; tus compañeros levantan sus workers (cada uno con su copia
local del corpus).

| Script | Para qué | Guarda en |
|---|---|---|
| `run_experiment.py` | Experimento de rendimiento (seq vs distribuido + correctitud) | `resultados/experimentos.csv` |
| `run_fault_test.py` | Caso de prueba con fallos de workers | `resultados/tolerancia_fallos.csv` |
| `graficar.py` | Gráficas de tiempos y speedup para el informe | `resultados/*.png` |

Dependencia: `requests` (ya está en `requirements.txt`). `graficar.py` además necesita
`matplotlib` (`pip install matplotlib`).

---

## 0. Un solo archivo, tamaño controlado desde el Coordinator

**No necesitas generar 5 archivos.** El tamaño del corpus se controla con
`--corpus-gb`: el Coordinator limita el corpus lógico a los primeros N GB del archivo
y reparte offsets dentro de `[0, N GB)`. Los workers solo leen esos rangos; el resto se
ignora. Así, un único archivo (p. ej. el de 5 GB) sirve para los 5 experimentos.

Lo único imprescindible: **el archivo debe ser idéntico byte a byte en todos los nodos**
(Coordinator y cada worker), porque el reparto es por rangos de bytes — el byte X tiene
que ser el mismo en todos lados. Verifica el tamaño en bytes en cada nodo
(`(Get-Item archivo).Length` en PowerShell, `stat -c%s archivo` en Linux) — debe
coincidir. Esta es la única preparación previa.

> Si tu archivo base no llega a 5 GB, concaténalo consigo mismo una vez en cada nodo
> hasta superar 5 GB (`cat base base > grande.txt`, o en PowerShell
> `Get-Content base | Add-Content grande.txt`), y distribuye ese archivo único.

---

## 1. Experimentos de rendimiento (corpus 1, 2, 3, 4, 5 GB)

Con los workers levantados y el archivo cargado en todos los nodos, corre cada tamaño
cambiando solo `--corpus-gb` (no toques los archivos entre corridas):

```bash
python experimentos/run_experiment.py --corpus-gb 1
python experimentos/run_experiment.py --corpus-gb 2
python experimentos/run_experiment.py --corpus-gb 3
python experimentos/run_experiment.py --corpus-gb 4
python experimentos/run_experiment.py --corpus-gb 5
```

Si tu Coordinator no está en localhost (lo corres en tu PC pero lanzas el script desde
otra), pásale la URL:

```bash
python experimentos/run_experiment.py --etiqueta 1GB --coordinator http://192.168.1.50:4999
```

Cada corrida agrega una fila a `resultados/experimentos.csv` con: tiempos secuencial y
distribuido, speedup, correctitud (✓/✗ contra el ground truth), palabras únicas y
reintentos. Esta es la tabla de la **Parte 4** del proyecto.

Al terminar las 5 corridas, genera las gráficas:

```bash
python experimentos/graficar.py
```

---

## 2. Casos de prueba con fallos

El fallo se induce **manualmente** durante la corrida (el script no puede tumbar workers
remotos). Formas de inducir un fallo en un worker:

- El dueño del worker lo detiene: `docker stop <contenedor>` o Ctrl-C en su terminal.
- Levantar un worker con `-e FAIL_MODE=true` → responde HTTP 500 siempre.
- Levantar un worker con `-e DELAY=<segundos>` → simula worker lento.

Para el **caso de recuperación**, vuelve a levantar el worker detenido: se auto-registra
y su Circuit Breaker pasa solo de OPEN → HALF_OPEN → CLOSED.

### Caso 1 — 1 worker cae a mitad del corpus de 1 GB

```bash
python experimentos/run_fault_test.py --caso 1 --corpus 1GB --corpus-gb 1 --workers-caidos 1 \
    --notas "worker_03 detenido al ~50% del procesamiento"
```

Cuando el script imprima `estado → running`, detén un worker. El sistema debe
redistribuir su fragmento y llegar al 100% con correctitud ✓.

### Caso 2 — 2 workers caen simultáneamente durante 3 GB

```bash
python experimentos/run_fault_test.py --caso 2 --corpus 3GB --corpus-gb 3 --workers-caidos 2 \
    --notas "worker_02 y worker_04 detenidos al mismo tiempo a la mitad"
```

### Caso 3 — 1 worker cae y se recupera (OPEN → HALF_OPEN → CLOSED)

```bash
python experimentos/run_fault_test.py --caso 3-recuperacion --corpus 1GB --corpus-gb 1 --workers-caidos 1 \
    --notas "worker_03 detenido al ~30% y vuelto a levantar al ~60%; CB recuperó a CLOSED"
```

Observa la transición del Circuit Breaker en tiempo real:

```bash
curl http://localhost:5005/workers/status   # mira el campo detalle.<worker>.state
```

Cada caso agrega una fila a `resultados/tolerancia_fallos.csv` con: tiempo total,
reintentos (reasignaciones de carga), correctitud y tus notas.

---

## Dónde quedan los resultados

Todo va a la carpeta `resultados/` en la raíz del repo:

```
resultados/
├── experimentos.csv        # tabla de rendimiento (Parte 4)
├── tolerancia_fallos.csv   # casos de prueba con fallos
├── tiempos.png             # gráfica seq vs distribuido
└── speedup.png             # gráfica de speedup
```

Estos CSV y PNG son la fuente de la tabla de resultados del README y de las secciones
de Resultados y Tolerancia a fallos del informe.
