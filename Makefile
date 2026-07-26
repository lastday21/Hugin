.PHONY: install format lint test web-check web-build check run desktop db-upgrade db-current db-check db-downgrade docker-build docker-up docker-down docker-logs precommit-install

install:
	uv sync --all-groups --all-extras

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

test:
	uv run pytest --cov=hugin --cov-report=term-missing

web-check:
	npm ci --prefix web
	npm run check --prefix web

web-build: web-check
	npm run build --prefix web

check: lint test web-build

run:
	uv run hugin

desktop:
	uv run hugin-desktop

db-upgrade:
	uv run hugin-db upgrade

db-current:
	uv run hugin-db current

db-check:
	uv run hugin-db check

db-downgrade:
	uv run hugin-db downgrade

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build --wait

docker-down:
	docker compose down

docker-logs:
	docker compose logs --tail=100 -f api

precommit-install:
	uv run pre-commit install --hook-type pre-push
