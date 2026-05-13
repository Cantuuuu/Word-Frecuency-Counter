# Distributed Word-Frequency Counter

Un sistema distribuido para contar la frecuencia de palabras en archivos de texto masivos (Wikipedia en español, ~5 GB) usando una arquitectura de microservicios con balanceo de carga, Circuit Breakers y reintentos automáticos.

El archivo **nunca viaja por red**: cada máquina tiene su copia local y solo se intercambian rangos de bytes y diccionarios de frecuencia (JSON pequeños).

---

## Arquitectura

```
Laptop A (Main)
┌─────────────────────────────────────────────┐
│  Coordinator  →  Ambassador (:5005)         │
│                      │                      │
│  .env: IPs de B, C, D                       │
└──────────────────────┬──────────────────────┘
          POST /count  │  (solo el Ambassador inicia conexiones)
          ┌────────────┼────────────┐
          ▼            ▼            ▼
     Laptop B      Laptop C      Laptop D
    Worker 1       Worker 2      Worker 3
    :5001          :5001         :5001
  wiki_es.txt    wiki_es.txt   wiki_es.txt
```

| Componente | Puerto | Rol |
|------------|--------|-----|
| Coordinator | — | Divide el archivo en chunks, despacha en paralelo, calcula Speedup |
| Ambassador | 5005 | Round-Robin inteligente, Circuit Breakers, reintentos, logging |
| Worker | 5001 | Lee su rango de bytes local, cuenta palabras, devuelve JSON |

---

## Requisitos

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) instalado en todas las laptops
- `wiki_es.txt` colocado en la raíz del repositorio en **cada laptop**
- Todas las laptops conectadas a la misma red WiFi

---

## Despliegue en 4 laptops

### Laptops Worker (B, C, D) — hacer primero

**1. Obtener la IP de la laptop:**
- Windows: `ipconfig` → buscar "Dirección IPv4" del adaptador Wi-Fi
- macOS: `ifconfig en0` → buscar la línea `inet`

**2. Abrir el puerto 5001 en el firewall:**

Windows (PowerShell como administrador):
```powershell
netsh advfirewall firewall add rule name="Worker 5001" protocol=TCP dir=in localport=5001 action=allow
```

macOS: Sistema → Privacidad y Seguridad → Firewall → Opciones → agregar Docker.

**3. Levantar el worker** (reemplazar `N` con 1, 2 o 3):
```bash
WORKER_ID=worker_N docker-compose -f docker-compose.worker.yml up --build
```

Verificar que el worker responde:
```bash
curl http://localhost:5001/health
# → {"status": "ok", "worker_id": "worker_N"}
```

---

### Laptop Principal (A) — hacer después de que B, C, D estén listos

**1. Configurar las IPs de los workers:**
```bash
cp .env.example .env
# Editar .env con las IPs reales de B, C, D:
# WORKER_1_URL=http://192.168.1.X:5001
# WORKER_2_URL=http://192.168.1.Y:5001
# WORKER_3_URL=http://192.168.1.Z:5001
```

**2. Levantar el Ambassador:**
```bash
make start
```

**3. Verificar que todos los servicios responden:**
```bash
make verify
```

Salida esperada:
```
✅  Ambassador  →  {"service":"ambassador","status":"ok"}
✅  Worker 1   →  {"status":"ok","worker_id":"worker_1"}
✅  Worker 2   →  {"status":"ok","worker_id":"worker_2"}
✅  Worker 3   →  {"status":"ok","worker_id":"worker_3"}
=== Resultado: 4 OK  /  0 FALLO(S) ===
```

**4. Lanzar el benchmark:**
```bash
make run
```

El Coordinator divide el archivo en 3 chunks, los despacha en paralelo al Ambassador, agrega los resultados y calcula el Speedup contra el conteo secuencial.

---

## Pruebas en una sola laptop (modo local)

Para probar sin red real, usar un worker local que recibe las 3 URLs:

```bash
# .env en modo local (las 3 URLs apuntan al mismo worker_local)
WORKER_1_URL=http://worker_local:5001
WORKER_2_URL=http://worker_local:5001
WORKER_3_URL=http://worker_local:5001
```

```bash
make start
make worker-local
make verify
make run
```

Simular el Coordinator manualmente (sin Docker):
```bash
python test_components.py   # envía 2 chunks de 100 MB, prueba el CB
```

---

## Referencia de comandos (Makefile)

| Comando | Descripción |
|---------|-------------|
| `make start` | Levanta el Ambassador |
| `make stop` | Detiene todos los contenedores |
| `make verify` | Verifica conectividad con Ambassador y los 3 workers |
| `make run` | Lanza el Coordinator (benchmark completo) |
| `make logs` | Sigue los logs del Ambassador en tiempo real |
| `make worker-local` | Levanta un worker local para pruebas |
| `make build` | Construye todas las imágenes sin levantar servicios |

---

## Comandos worker (laptops B, C, D)

| Comando | Descripción |
|---------|-------------|
| `WORKER_ID=worker_N docker-compose -f docker-compose.worker.yml up --build` | Levantar worker |
| `docker-compose -f docker-compose.worker.yml down` | Detener worker |
| `curl http://localhost:5001/health` | Verificar que el worker responde |

---

## Decisiones de diseño

- **Puerto 5005 para el Ambassador:** El puerto 5000 lo usa el AirPlay Receiver de macOS.
- **Timeout de 20s:** Tolera la virtualización de disco de Docker en laptops con discos lentos.
- **Fast-fail con Circuit Breaker:** Si un worker no responde, su CB pasa a OPEN y el Ambassador lo excluye inmediatamente del Round-Robin.
- **Regex `[a-záéíóúüñ]+`:** Solo letras del español; excluye números, guiones y caracteres no-latinos presentes en el texto de Wikipedia.
- **`errors="replace"` en UTF-8:** Los chunks se cortan en límites de byte, no de carácter. `replace` evita excepciones en caracteres multi-byte fragmentados.

---

*Proyecto universitario — Sistemas Distribuidos.*
