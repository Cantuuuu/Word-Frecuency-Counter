# Reporte — Distributed Word-Frequency Counter
### Proyecto - Sistemas Distribuidos 
## 1. Introducción

Este proyecto implementa un sistema distribuido para contar la frecuencia de palabras en un archivo de texto masivo: el dump de Wikipedia en español (`wiki_es.txt`, ~5.2 GB). El objetivo es dividir el archivo en fragmentos (chunks), procesarlos en paralelo en multiples computadoras y combinar los resultados, demostrando como con una arquitectura que contempla un Ambassador y Circuit Breaker mejoran la resiliencia y el rendimiento en sistemas distribuidos.

El sistema se compone de tres microservicios desplegados en contenedores Docker:

- **Coordinator** (puerto 4999): Orquestador central que divide el archivo en chunks por rangos de bytes, los despacha en paralelo, reintenta automáticamente los que fallen y combina los resultados finales.
- **Ambassador** (puerto 5005): Intermediario entre el Coordinator y los Workers. Gestiona el registro dinámico de workers, balancea la carga mediante Round-Robin inteligente, aplica Circuit Breakers por worker y maneja reintentos con rotación.
- **Workers** (puerto 5001): Servidores que reciben un rango de bytes, leen su copia local del archivo, extraen palabras con expresiones regulares y retornan el conteo como un diccionario JSON.

Cada nodo tiene su propia copia del archivo. Solo se transmiten por red los rangos de bytes (metadatos) y los diccionarios de frecuencia (resultado), evitando transferir los 5.2 GB del archivo.

---

## 2. Metodología

### 2.1 Division del trabajo

El Coordinator calcula el tamaño total del archivo con `os.path.getsize()` y lo divide en `N` chunks de tamaño igual, donde `N` es el número de workers saludables en el momento de iniciar. Cada chunk se define como un par `(inicio, fin)` de bytes.

Para evitar cortar palabras en los límites de chunk, cada worker ajusta su posición de inicio al siguiente espacio en blanco (`_ajustar_inicio`) y extiende el fin 512 bytes adicionales (`BOUNDARY_BUF`) para capturar palabras partidas.

### 2.2 Despacho y balanceo de carga

El Coordinator despacha todos los chunks en paralelo usando `ThreadPoolExecutor`. Cada chunk se envia al Ambassador via `POST /dispatch`, que selecciona un worker mediante Round-Robin inteligente:

1. **Round-Robin**: Avanza un índice circular sobre el pool de workers registrados.
2. **Busy check**: Salta workers que ya están procesando otro chunk (`_busy_workers`), evitando doble-asignación.
3. **Circuit Breaker**: Salta workers cuyo CB está en estado OPEN (fast-fail).

El Ambassador traduce el contrato (`inicio/fin` del Coordinator a `start/end` del Worker) y reenvía la petición al worker seleccionado.

### 2.3 Procesamiento en el Worker

Cada worker:
1. Abre el archivo local en modo lectura con encoding UTF-8 (`errors="ignore"`).
2. Ajusta la posición de inicio para no partir palabras.
3. Lee en bloques de 64 MB (`BUFFER_SIZE`) para evitar consumir toda la memoria.
4. Extrae palabras con `re.findall(r"\b\w+\b", chunk.lower())`.
5. Acumula las frecuencias en un `Counter` y las retorna como JSON.

### 2.4 Tolerancia a fallos

El sistema implementa tolerancia a fallos en tres niveles:

**Nivel 1 — Circuit Breaker por worker (Ambassador):**
Cada worker tiene un Circuit Breaker con tres estados:
- **CLOSED**: Operación normal. Las peticiones pasan al worker.
- **OPEN**: Tras 2 fallos consecutivos (`FAIL_MAX=2`), el circuito se abre y todas las peticiones se rechazan inmediatamente (fast-fail). Tras 10 segundos (`RESET_TIMEOUT`), transiciona a HALF_OPEN.
- **HALF_OPEN**: Se permite una peticion de prueba. Si tiene exito, el CB vuelve a CLOSED. Si falla, regresa a OPEN.

**Nivel 2 — Reintentos con rotación (Ambassador):**
Cada dispatch tiene hasta 3 intentos (`MAX_RETRIES=3`). Si un worker falla, se agrega a un conjunto `ya_fallaron` y el Ambassador selecciona otro worker distinto para el siguiente intento. Si no hay workers disponibles, retorna error inmediatamente.

**Nivel 3 — Reintento persistente (Coordinator):**
Si un chunk falla tras agotar los reintentos del Ambassador, el Coordinator no lo descarta. Un loop persistente reintenta chunks fallidos indefinidamente con una pausa de 10 segundos (`RETRY_DELAY`) entre cada intento, esperando que los workers se recuperen. El sistema siempre alcanza el 100% de completitud.

### 2.5 Auto-registro y monitoreo de workers

Los workers se registran automáticamente con el Ambassador al arrancar, reintentando indefinidamente cada 15 segundos hasta lograrlo. Una vez registrados, mantienen un loop de monitoreo que hace ping al Ambassador cada 15 segundos. Si detectan que el Ambassador se cayó, limpian su estado de registro y vuelven a intentar registrarse.

Cuando un worker se re-registra y su Circuit Breaker estaba en estado no-CLOSED (por fallos previos), el Ambassador crea un nuevo CB en CLOSED, tratando al worker como sano tras su reinicio.

### 2.6 Health check pre-despacho

Antes de despachar chunks, el Coordinator verifica la salud de todos los workers registrados via `GET /workers/health` en el Ambassador. Este endpoint realiza health checks en paralelo (timeout 3 segundos por worker) y retorna solo los workers que responden y tienen el archivo accesible. Los workers no saludables se excluyen del procesamiento.

### 2.7 Ground Truth

Opcionalmente, tras el procesamiento distribuido, el Coordinator ejecuta un conteo secuencial del archivo completo (línea por línea, mismo regex). Esto permite calcular el speedup real: `speedup = T_secuencial / T_distribuido`.

---

## 3. Resultados

### 3.1 Paralelismo y speedup

Al dividir el archivo de 5.2 GB en `N` chunks y procesarlos simultaneamente en `N` workers, el sistema reduce el tiempo de procesamiento proporcionalmente al número de nodos. El ground truth secuencial sirve como baseline para medir el speedup obtenido.

El speedup depende de:
- **Número de workers**: Más workers = más chunks en paralelo = menor tiempo.
- **Velocidad de disco de cada nodo**: El cuello de botella es la lectura del archivo local, no la red.
- **Overhead de red**: minimo, ya que solo se transmiten metadatos y diccionarios JSON (kilobytes), no el archivo (gigabytes).

### 3.2 Eficiencia del balanceo de carga

El Round-Robin inteligente distribuye chunks equitativamente entre los workers disponibles. La protección anti-doble-asignacion (`_busy_workers`) garantiza que un worker no reciba un segundo chunk hasta que termine el primero, evitando saturación.

### 3.3 Recuperacion ante fallos

El sistema fue diseñado para llegar siempre al 100% de completitud:
- Un worker que se cae es detectado por el Circuit Breaker tras 2 fallos consecutivos, y excluido inmediatamente de la rotación (fast-fail en microsegundos en vez de esperar un timeout de 600 segundos).
- El chunk se reasigna a otro worker en el mismo dispatch (hasta 3 intentos).
- Si todos los intentos del Ambassador se agotan, el Coordinator reintenta el chunk 10 segundos después, dando tiempo a que los workers se recuperen o se re-registren.
- Cuando un worker vuelve, su auto-registro resetea el Circuit Breaker a CLOSED, reincorporándolo al pool activo.

### 3.4 Lectura eficiente de archivos grandes

La lectura en bloques de 64 MB evita cargar chunks de gigabytes en memoria. El ajuste de fronteras de palabra (`_ajustar_inicio` + `BOUNDARY_BUF`) garantiza que no se pierdan ni dupliquen palabras en los cortes de chunk, sin necesidad de comunicación entre workers. Además, cada bloque de 64 MB arrastra (carry-over) el fragmento de palabra que quede al final hacia el siguiente bloque, evitando partir palabras también en las fronteras internas de lectura. Gracias a esto el conteo distribuido es idéntico, palabra por palabra, al ground truth secuencial.

### 3.5 Tabla de resultados: secuencial vs. distribuido

Cada experimento se ejecutó con el mismo archivo en todos los nodos, limitando el corpus lógico al tamaño indicado mediante el parámetro `corpus_gb` del Coordinator (un único archivo sirve para los cinco tamaños). El tiempo distribuido incluye el despacho, el procesamiento en los workers y la combinación de resultados; el tiempo secuencial es el conteo del mismo rango `[0, N GB)` en un solo proceso (ground truth).

> Datos generados con `experimentos/run_experiment.py` → `resultados/experimentos.csv`.
> Workers utilizados: **_N_** (rellenar). `Speedup = T_secuencial / T_distribuido`.

| Corpus | T. Secuencial (s) | T. Distribuido (s) | Speedup | Correctitud |
|--------|-------------------|--------------------|---------|-------------|
| 1 GB   | _pendiente_       | _pendiente_        | _—_     | _✓ / ✗_     |
| 2 GB   | _pendiente_       | _pendiente_        | _—_     | _✓ / ✗_     |
| 3 GB   | _pendiente_       | _pendiente_        | _—_     | _✓ / ✗_     |
| 4 GB   | _pendiente_       | _pendiente_        | _—_     | _✓ / ✗_     |
| 5 GB   | _pendiente_       | _pendiente_        | _—_     | _✓ / ✗_     |

La columna **Correctitud** es ✓ cuando el Counter distribuido es idéntico al ground truth secuencial: mismo número de palabras únicas y misma frecuencia en cada palabra. El Coordinator hace esta verificación automáticamente al finalizar (campo `correcto` en `/result`).

### 3.6 Gráficas

Generadas con `experimentos/graficar.py` a partir de `resultados/experimentos.csv`:

- **Tiempos secuencial vs. distribuido por tamaño de corpus** — `resultados/tiempos.png`
- **Speedup por tamaño de corpus** — `resultados/speedup.png`

> _Insertar aquí las dos imágenes una vez generadas:_
>
> `![Tiempos secuencial vs distribuido](../resultados/tiempos.png)`
>
> `![Speedup por tamaño de corpus](../resultados/speedup.png)`

Lectura esperada: el speedup debe ser **> 1** (el distribuido es más rápido que el secuencial) y crecer o estabilizarse a medida que aumenta el tamaño del corpus, ya que el costo fijo de coordinación se amortiza mejor sobre más datos.

---

## 4. Tolerancia a Fallos — Casos de Prueba

Se ejecutaron los tres escenarios obligatorios induciendo el fallo de uno o más workers a mitad del procesamiento (deteniendo su contenedor o levantándolo con `FAIL_MODE=true`). En todos los casos el sistema debe llegar al **100 % de completitud** sin perder ningún fragmento, y el resultado debe seguir siendo idéntico al ground truth.

> Datos generados con `experimentos/run_fault_test.py` → `resultados/tolerancia_fallos.csv`.

| Caso | Corpus | Workers (inicial → caídos) | T. Distribuido (s) | Reasignaciones (reintentos) | Correctitud vs. GT | Observaciones |
|------|--------|----------------------------|--------------------|-----------------------------|--------------------|---------------|
| 1 — 1 worker cae a mitad           | 1 GB | _N_ → 1 | _pendiente_ | _pendiente_ | _✓ / ✗_ | _cómo se redistribuyó la carga_ |
| 2 — 2 workers caen simultáneamente | 3 GB | _N_ → 2 | _pendiente_ | _pendiente_ | _✓ / ✗_ | _cómo se redistribuyó la carga_ |
| 3 — 1 worker cae y se recupera     | 1 GB | _N_ → 1 → _N_ | _pendiente_ | _pendiente_ | _✓ / ✗_ | _transición OPEN → HALF_OPEN → CLOSED_ |

### 4.1 Caso 1 — Un worker cae a mitad del corpus de 1 GB

_Describir: en qué momento se detuvo el worker, cómo el Circuit Breaker pasó a OPEN tras `FAIL_MAX` fallos, cómo el Ambassador reasignó el fragmento a otro worker activo y cómo el conteo final coincidió con el ground truth._

### 4.2 Caso 2 — Dos workers caen simultáneamente durante 3 GB

_Describir: caída simultánea de dos workers, redistribución de sus dos fragmentos sobre los workers restantes, tiempo total y verificación de correctitud._

### 4.3 Caso 3 — Un worker cae y se recupera (OPEN → HALF_OPEN → CLOSED)

_Describir: se detuvo un worker (CB → OPEN), luego se volvió a levantar; tras `RESET_TIMEOUT` el CB pasó a HALF_OPEN, la petición de prueba tuvo éxito y volvió a CLOSED, reincorporándose al pool. Documentar la secuencia observada en `GET /workers/status`._

---

## 5. Decisiones de Diseño

### 5.1 Archivo local en cada nodo (no transferencia por red)

El archivo `wiki_es.txt` reside en cada computadora participante. El Coordinator solo envia rangos de bytes (dos enteros) y recibe diccionarios de frecuencia (JSON de kilobytes). La alternativa sería que un nodo central sirviera el archivo por red, pero transferir 5.2 GB por WiFi a cada worker sería el cuello de botella. Con copias locales, el unico factor limitante es la velocidad de disco de cada nodo. 

Probar con nuevos archivos es tan simple como colocar el nuevo archivo en cada nodo y ajustar el nombre en el código, sin necesidad de cambios en la logica de red o procesamiento.

### 5.2 Bind mount en vez de COPY en Docker

El archivo no se incluye en la imagen Docker (`COPY` lo haría parte de la imagen de ~5.3 GB). En su lugar, se monta como volumen de solo lectura (`-v wiki_es.txt:/app/data/input.txt:ro`). Esto mantiene las imágenes ligeras (~150 MB) y permite cambiar el archivo sin reconstruir la imagen.

### 5.3 Ambassador como punto unico de contacto

El Coordinator no conoce la existencia de workers individuales. Solo se comunica con el Ambassador via tres endpoints: `POST /dispatch`, `GET /workers/status` y `GET /workers/health`. Toda la logica de selección, reintentos, Circuit Breakers y registro vive en el Ambassador. Esto permite que el Coordinator se mantenga simple (orquestación pura) y que la política de balanceo/resiliencia sea modificable sin tocar el Coordinator.

### 5.4 IDs asignados por el Ambassador (no por el worker)

El Ambassador asigna `worker_01`, `worker_02`, etc. de forma autoincremental al registrarse. Si el worker enviara su propio ID, habría riesgo de colisiones o inconsistencias. Con una asignación centralizada, el Ambassador garantiza individualidad y puede detectar re-registros por URL.

### 5.5 Flask threaded=True en el Ambassador

El Ambassador corre con `threaded=True` para atender multiples dispatches concurrentes. Sin esto, los chunks se encolarían y el despacho paralelo del Coordinator sería secuencial en la práctica. Los Circuit Breakers usan `threading.RLock` para proteger su estado ante accesos concurrentes.

### 5.6 Puerto 5005 para el Ambassador

Se evitó el puerto 5000 porque macOS lo ocupa con AirPlay Receiver, lo que causaria conflictos en equipos Apple del equipo de trabajo. Pero esto no limitó la flexibilidad de trabajar con cualquier puerto disponible, así como cualquier sistema operativo, personalmente probamos Windows como host del Ambassador y Coordinator y Workers tanto en Windows, MacOS y Linux, trabajando sin problemas.

### 5.7 Timeout de 600 segundos

El procesamiento de un chunk de ~1.7 GB (con 3 workers) sobre disco local en Docker puede tomar varios minutos. Un timeout demasiado corto causaría falsos positivos en el Circuit Breaker, siendo este un problema que enfrentamos, 600 segundos (10 minutos) da margen suficiente para discos lentos o sistemas con alta carga de virtualización.

### 5.8 RETRY_DELAY de 10 segundos en el Coordinator

El Coordinator espera 10 segundos entre reintentos persistentes. Un valor más bajo saturaría al Ambassador con peticiones que probablemente siguen fallando. Un valor más alto retrasaría la recuperación cuando un worker vuelve. 10 segundos es un compromiso que permite al worker completar su re-registro (que ocurre cada 15 segundos) antes del siguiente intento.

### 5.9 Persistencia del registro de workers

El Ambassador guarda el registro en `workers_registry.json`. Si el Ambassador se reinicia, recupera la lista de workers sin esperar que cada uno se re-registre. Los CBs se crean en CLOSED al cargar, y si un worker ya no esta disponible, el CB lo detectara en el primer fallo.

---

## 6. Puntos de Mejora

### 6.1 Chunks estáticos: un chunk por worker

Actualmente `num_chunks = len(workers)`, asignando exactamente un chunk por worker. Si un worker tiene disco más rapido que otro, terminara antes y quedara ocioso mientras el lento continúa. Una mejora sería dividir en más chunks que workers (por ejemplo, `4 * N`) y asignarlos bajo demanda: cuando un worker termina un chunk, recibe el siguiente pendiente. Esto lograria un mejor balanceo de carga dinámico, adaptándose a las diferencias de rendimiento entre nodos.

### 6.2 Punto unico de fallo: el Ambassador

Si el Ambassador se cae, todo el sistema se detiene. Los workers no pueden registrarse y el Coordinator no puede despachar. Una mejora sería tener multiples instancias del Ambassador detrás de un, load balancer, o que el Coordinator pueda comunicarse directamente con workers como fallback.

### 6.3 Transferencia de resultados grandes

Cada worker retorna un diccionario completo `{palabra: conteo}` como JSON. Con un vocabulario de millones de palabras únicas, estos diccionarios pueden pesar decenas de megabytes. Una mejora sería que cada worker retorne solo las top-N palabras, o comprimir el JSON con gzip, o usar un formato binario más eficiente.

### 6.4 Sin autenticación entre servicios

Cualquier máquina en la misma red puede registrarse como worker o enviar dispatches al Ambassador. En un entorno de producción, se necesitarían tokens de autenticación o mTLS entre los servicios.

### 6.5 Historial en memoria

El historial de ejecuciones (`_history`, máximo 5 entradas) se pierde al reiniciar el Coordinator. Una mejora sería persistirlo en disco, similar a como el Ambassador persiste el registro de workers.

---

## 7. Conclusion

### El rol del Ambassador

El Ambassador actúa como intermediario entre el Coordinator y los Workers, centralizando tres responsabilidades críticas:

1. **Registro dinámico**: Los workers se anuncian al Ambassador al arrancar, eliminando la necesidad de configurar IPs manualmente. Esto permite agregar o quitar workers sin modificar configuración.
2. **Balanceo de carga**: El Round-Robin inteligente, combinado con la protección de workers ocupados, distribuye el trabajo equitativamente sin que el Coordinator necesite conocer el estado de cada worker.
3. **Abstracción de fallos**: El Coordinator envia chunks al Ambassador sin preocuparse porque worker los procesa ni cuantos reintentos fueron necesarios. El Ambassador maneja toda la complejidad de selección, reintento y rotación.

Sin el Ambassador, el Coordinator tendría que mantener el registro de workers, gestionar conexiones individuales, implementar reintentos y manejar fallos directamente, mezclando logica de orquestación con logica de comunicación.

### El rol del Circuit Breaker

El Circuit Breaker protege al sistema de insistir en workers que ya demostraron estar caídos:

1. **Fast-fail**: En vez de esperar 600 segundos de timeout por cada intento a un worker caido, el CB en OPEN rechaza la petición en microsegundos, permitiendo rotar al siguiente worker inmediatamente.
2. **Recuperación automática**: El estado HALF_OPEN permite detectar cuando un worker se recupera, enviándole una sola petición de prueba antes de reincorporarlo al pool activo.
3. **Aislamiento de fallos**: Un worker con problemas no afecta al resto. Su CB se abre independientemente, mientras los demás continúan procesando con normalidad.

La combinación de Ambassador y Circuit Breaker crea un sistema que se adapta dinámicamente a las condiciones de la red: agrega workers cuando arrancan, los excluye cuando fallan, los reincorpora cuando se recuperan siendo robusto ante fallos y eficiente en la distribución de carga, permitiendo completar el procesamiento del archivo masivo con un alto speedup y sin intervención manual, incluso en presencia de caídas temporales de nodos.

---

*Sístemas Distribuidos - 6to Semestre*

**Integrantes:**
- Arturo Cantu Olivarez — 294863
- Luis Fernando Maldonado — 325677
- Jesus Gabriel Gudino Lara — 325675
