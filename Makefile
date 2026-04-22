.PHONY: dev-up dev-down sandbox-build migrate

dev-up:
	docker compose -f docker-compose.dev.yml up -d

dev-down:
	docker compose -f docker-compose.dev.yml down

sandbox-build:
	docker build -t ada-sandbox:latest sandbox/

migrate:
	alembic upgrade head
