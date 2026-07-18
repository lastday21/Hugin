.PHONY: install format lint test check run docker-build docker-up docker-down docker-logs precommit-install

install:
	uv sync --all-groups

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

test:
	uv run pytest --cov=hugin --cov-report=term-missing

check: lint test

run:
	uv run hugin

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
