# Makefile — Comandos para el día de demo.
# Laptop principal (Ambassador + Coordinator).
#
# Uso rápido:
#   make start     → levanta el Ambassador (listo para recibir chunks)
#   make verify    → verifica que Ambassador + 3 workers respondan
#   make run       → lanza el coordinator (benchmark distribuido + secuencial)
#   make stop      → detiene todos los contenedores
#   make logs      → sigue los logs del Ambassador en tiempo real

.PHONY: start stop run verify logs build worker-local

## Levanta el Ambassador. No lanza el Coordinator (se lanza manualmente con 'make run').
start:
	docker-compose up -d --build ambassador

## Detiene y elimina todos los contenedores del proyecto.
stop:
	docker-compose --profile coordinator down

## Verifica conectividad con Ambassador y los 3 workers antes del benchmark.
verify:
	@bash scripts/verify-network.sh

## Lanza el Coordinator (benchmark distribuido + ground truth secuencial).
## Requiere: make start + make verify pasen sin errores.
run:
	docker-compose --profile coordinator run --rm coordinator

## Sigue los logs del Ambassador en tiempo real.
logs:
	docker-compose logs -f ambassador

## Levanta un worker local para pruebas en una sola laptop (sin red real).
worker-local:
	docker-compose up -d --build worker_local

## Construye todas las imágenes sin levantar servicios.
build:
	docker-compose --profile coordinator build
