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

## 3. Resultados de la Optimizacion

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

La lectura en bloques de 64 MB evita cargar chunks de gigabytes en memoria. El ajuste de fronteras de palabra (`_ajustar_inicio` + `BOUNDARY_BUF`) garantiza que no se pierdan ni dupliquen palabras en los cortes de chunk, sin necesidad de comunicación entre workers.

---

## 4. Decisiones de Diseño

### 4.1 Archivo local en cada nodo (no transferencia por red)

El archivo `wiki_es.txt` reside en cada computadora participante. El Coordinator solo envia rangos de bytes (dos enteros) y recibe diccionarios de frecuencia (JSON de kilobytes). La alternativa sería que un nodo central sirviera el archivo por red, pero transferir 5.2 GB por WiFi a cada worker sería el cuello de botella. Con copias locales, el unico factor limitante es la velocidad de disco de cada nodo. 

Probar con nuevos archivos es tan simple como colocar el nuevo archivo en cada nodo y ajustar el nombre en el código, sin necesidad de cambios en la logica de red o procesamiento.

### 4.2 Bind mount en vez de COPY en Docker

El archivo no se incluye en la imagen Docker (`COPY` lo haría parte de la imagen de ~5.3 GB). En su lugar, se monta como volumen de solo lectura (`-v wiki_es.txt:/app/data/input.txt:ro`). Esto mantiene las imágenes ligeras (~150 MB) y permite cambiar el archivo sin reconstruir la imagen.

### 4.3 Ambassador como punto unico de contacto

El Coordinator no conoce la existencia de workers individuales. Solo se comunica con el Ambassador via tres endpoints: `POST /dispatch`, `GET /workers/status` y `GET /workers/health`. Toda la logica de selección, reintentos, Circuit Breakers y registro vive en el Ambassador. Esto permite que el Coordinator se mantenga simple (orquestación pura) y que la política de balanceo/resiliencia sea modificable sin tocar el Coordinator.

### 4.4 IDs asignados por el Ambassador (no por el worker)

El Ambassador asigna `worker_01`, `worker_02`, etc. de forma autoincremental al registrarse. Si el worker enviara su propio ID, habría riesgo de colisiones o inconsistencias. Con una asignación centralizada, el Ambassador garantiza individualidad y puede detectar re-registros por URL.

### 4.5 Flask threaded=True en el Ambassador

El Ambassador corre con `threaded=True` para atender multiples dispatches concurrentes. Sin esto, los chunks se encolarían y el despacho paralelo del Coordinator sería secuencial en la práctica. Los Circuit Breakers usan `threading.RLock` para proteger su estado ante accesos concurrentes.

### 4.6 Puerto 5005 para el Ambassador

Se evitó el puerto 5000 porque macOS lo ocupa con AirPlay Receiver, lo que causaria conflictos en equipos Apple del equipo de trabajo. Pero esto no limitó la flexibilidad de trabajar con cualquier puerto disponible, así como cualquier sistema operativo, personalmente probamos Windows como host del Ambassador y Coordinator y Workers tanto en Windows, MacOS y Linux, trabajando sin problemas.

### 4.7 Timeout de 600 segundos

El procesamiento de un chunk de ~1.7 GB (con 3 workers) sobre disco local en Docker puede tomar varios minutos. Un timeout demasiado corto causaría falsos positivos en el Circuit Breaker, siendo este un problema que enfrentamos, 600 segundos (10 minutos) da margen suficiente para discos lentos o sistemas con alta carga de virtualización.

### 4.8 RETRY_DELAY de 10 segundos en el Coordinator

El Coordinator espera 10 segundos entre reintentos persistentes. Un valor más bajo saturaría al Ambassador con peticiones que probablemente siguen fallando. Un valor más alto retrasaría la recuperación cuando un worker vuelve. 10 segundos es un compromiso que permite al worker completar su re-registro (que ocurre cada 15 segundos) antes del siguiente intento.

### 4.9 Persistencia del registro de workers

El Ambassador guarda el registro en `workers_registry.json`. Si el Ambassador se reinicia, recupera la lista de workers sin esperar que cada uno se re-registre. Los CBs se crean en CLOSED al cargar, y si un worker ya no esta disponible, el CB lo detectara en el primer fallo.

---

## 5. Puntos de Mejora

### 5.1 Chunks estáticos: un chunk por worker

Actualmente `num_chunks = len(workers)`, asignando exactamente un chunk por worker. Si un worker tiene disco más rapido que otro, terminara antes y quedara ocioso mientras el lento continúa. Una mejora sería dividir en más chunks que workers (por ejemplo, `4 * N`) y asignarlos bajo demanda: cuando un worker termina un chunk, recibe el siguiente pendiente. Esto lograria un mejor balanceo de carga dinámico, adaptándose a las diferencias de rendimiento entre nodos.

### 5.2 Punto unico de fallo: el Ambassador

Si el Ambassador se cae, todo el sistema se detiene. Los workers no pueden registrarse y el Coordinator no puede despachar. Una mejora sería tener multiples instancias del Ambassador detrás de un, load balancer, o que el Coordinator pueda comunicarse directamente con workers como fallback.

### 5.3 Transferencia de resultados grandes

Cada worker retorna un diccionario completo `{palabra: conteo}` como JSON. Con un vocabulario de millones de palabras únicas, estos diccionarios pueden pesar decenas de megabytes. Una mejora sería que cada worker retorne solo las top-N palabras, o comprimir el JSON con gzip, o usar un formato binario más eficiente.

### 5.4 Sin autenticación entre servicios

Cualquier máquina en la misma red puede registrarse como worker o enviar dispatches al Ambassador. En un entorno de producción, se necesitarían tokens de autenticación o mTLS entre los servicios.

### 5.5 Historial en memoria

El historial de ejecuciones (`_history`, máximo 5 entradas) se pierde al reiniciar el Coordinator. Una mejora sería persistirlo en disco, similar a como el Ambassador persiste el registro de workers.

---

## 6. Conclusion

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
