# Distributed Word-Frequency Counter 📊

Un sistema distribuido robusto para contar la frecuencia de palabras en archivos de texto masivos (como la Wikipedia en español de 5.2 GB) utilizando una arquitectura de microservicios con balanceo de carga, tolerancia a fallos y reintentos automáticos.

## 🏗️ Arquitectura del Sistema

El sistema utiliza un enfoque distribuido descentralizando el procesamiento en múltiples computadoras (nodos), evitando enviar el archivo pesado por red:

1. **Coordinator** (Puerto 4999): Divide el archivo en bloques (rangos de bytes) y asigna las tareas.
2. **Ambassador** (Puerto 5005): Actúa como proxy y balanceador de carga inteligente. Implementa Circuit Breakers para detectar workers caídos, hace *Round-Robin* dinámico y gestiona políticas de reintento.
3. **Workers** (Puerto 5001): Servidores Flask que reciben el rango de bytes, leen su disco local, extraen las palabras usando expresiones regulares seguras (Regex) y retornan el conteo final.

> **Dato Clave:** El archivo `wiki_es.txt` reside localmente en cada nodo. El sistema solo intercambia metadata por red (offsets de inicio/fin y JSONs con diccionarios de frecuencia).

## 🚀 Tecnologías
* **Lenguaje:** Python 3.11
* **Web Framework:** Flask (servidores REST multi-hilo)
* **Comunicación HTTP:** Librería `requests`
* **Infraestructura:** Docker y Docker Compose

---

## ⚙️ Requisitos Previos

1. Instalar [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. Instalar [Python 3.11+](https://www.python.org/).
3. Colocar el archivo `wiki_es.txt` en la raíz del proyecto.

---

## 🖥️ Instrucciones de Uso

### 1. Preparación del Entorno
Clona el repositorio e instala las dependencias de Python si deseas hacer pruebas locales:
```bash
pip install -r requierements.txt
```

Para usar la red en vivo, copia el archivo de variables de entorno y ajusta las IPs de los workers:
```bash
cp .env.example .env
```

### 2. Ejecutar con Docker (Recomendado)
Levantar el Ambassador y un Worker local de prueba. El archivo de 5GB se monta automáticamente usando *bind mounts* para ahorrar espacio:
```bash
docker-compose up --build
```
Para detener los contenedores:
```bash
docker-compose down
```

### 3. Pruebas y Validación (Mock)
Si el Coordinator no está listo, puedes simularlo para probar el flujo de red, el Ambassador y los workers:
```bash
python test_components.py
```
Este script envía dos bloques de 100 MB y comprueba la lógica de Circuit Breaker.

### 4. Extraer el Ground Truth (Línea Base)
Para calcular el porcentaje de mejora o *Speedup* del sistema distribuido, primero debes medir cuánto tarda el procesamiento secuencial en un solo hilo:
```bash
python ground_truth.py
```

---

## 🛠️ Decisiones de Diseño Críticas
* **Puerto 5005 para el Ambassador:** Modificado del estándar 5000 para evitar conflictos con el *AirPlay Receiver* de macOS.
* **Tolerancia a Discos Lentos:** El timeout del Ambassador está configurado en `20 segundos` (`config.py`) para evitar fallas tempranas al leer de la virtualización de archivos de Docker.
* **Fast-Fail:** Si un worker no responde (Timeout o Connection Error), el Circuit Breaker entra en estado `OPEN` y el Ambassador lo ignora instantáneamente en las siguientes peticiones.

---
*Proyecto Universitario de Sistemas Distribuidos.*
