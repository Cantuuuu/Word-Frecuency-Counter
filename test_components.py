"""
test_components.py — Script para probar el Ambassador y Worker sin el Coordinator.

Este script simula ser el Coordinator. Envía algunos chunks de prueba
al Ambassador para verificar que el ruteo, el conteo y la respuesta JSON
funcionan de principio a fin.

Instrucciones:
  1. Abre otra terminal y levanta los servicios:
     docker-compose up --build
  2. Ejecuta este script:
     python test_components.py
"""

import requests
import time
import os

AMBASSADOR_URL = "http://localhost:5005"
FILE_SIZE = os.path.getsize("wiki_es.txt") if os.path.exists("wiki_es.txt") else 5000000

def probar_salud():
    print(f"1️⃣ Comprobando salud de los workers...")
    try:
        resp = requests.get(f"{AMBASSADOR_URL}/workers/status", timeout=2)
        resp.raise_for_status()
        data = resp.json()
        print(f"   Workers activos: {data.get('activos')}")
        return len(data.get("activos", [])) > 0
    except Exception as e:
        print(f"   ❌ Error contactando al Ambassador: {e}")
        print("   ¿Están corriendo los contenedores? (docker-compose up)")
        return False

def enviar_chunk(chunk_id, inicio, fin):
    print(f"\n2️⃣ Enviando Chunk #{chunk_id} (Bytes {inicio} -> {fin})...")
    payload = {
        "chunk_id": chunk_id,
        "inicio": inicio,
        "fin": fin
    }
    
    t0 = time.monotonic()
    try:
        resp = requests.post(f"{AMBASSADOR_URL}/dispatch", json=payload, timeout=60)
        t1 = time.monotonic()
        
        if resp.status_code == 200:
            data = resp.json()
            worker_usado = data.get("worker_id")
            resultados = data.get("result", {})
            total_palabras = sum(resultados.values())
            
            print(f"   ✅ OK! Procesado por {worker_usado} en {t1 - t0:.2f}s")
            print(f"   📊 Palabras distintas en este chunk: {len(resultados)}")
            print(f"   📈 Total de palabras sumadas: {total_palabras}")
        else:
            print(f"   ❌ Fallo (HTTP {resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"   ❌ Error en la petición POST: {e}")

def main():
    print("🚀 Iniciando prueba de componentes (Mock Coordinator)")
    print("-" * 50)
    
    if not probar_salud():
        return
        
    # Chunk pequeño de prueba (100 MB)
    tamano_chunk = 100 * 1024 * 1024 
    fin_chunk = min(tamano_chunk, FILE_SIZE)
    enviar_chunk(1, 0, fin_chunk)
    
    # Otro chunk de prueba
    if FILE_SIZE > tamano_chunk:
        enviar_chunk(2, fin_chunk, min(fin_chunk + tamano_chunk, FILE_SIZE))

    print("\n3️⃣ Revisando estado de los Circuit Breakers...")
    resp = requests.get(f"{AMBASSADOR_URL}/workers/status")
    if resp.status_code == 200:
        import json
        print(json.dumps(resp.json(), indent=2))
        
    print("\n✅ Pruebas finalizadas.")

if __name__ == "__main__":
    main()
