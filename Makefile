.PHONY: install format lint test check run precommit-install

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

precommit-install:
	uv run pre-commit install --hook-type pre-push
