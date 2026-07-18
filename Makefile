install:
	\tpoetry install --with dev

lint:
	\tpoetry run ruff check .
	\tpoetry run ruff format --check .
	\tpoetry run mypy --config-file pyproject.toml .

test:
	\tpoetry run pytest -m "not integration"

test-integration:
	\tpoetry run pytest -m "integration"

precommit-install:
	\tpoetry run pre-commit install --hook-type pre-push
