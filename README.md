# Hugin

Hugin — местный персональный агент поиска работы. Сейчас создана основа серверной части: настройки, фабрика приложения FastAPI и проверка состояния `/health`.

Полное техническое задание и история его уточнений находятся в каталоге `docs/requirements`.

## Требования

- Python 3.12;
- uv 0.11 или новее.

## Подготовка

```powershell
uv sync --all-groups
Copy-Item .env.example .env
```

## Запуск

```powershell
uv run hugin
```

После запуска проверка состояния доступна по адресу `http://127.0.0.1:8000/health`.

## Проверки

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=hugin --cov-report=term-missing
```

Рабочая база, журналы, профиль браузера, снимки и трассировки не должны находиться в репозитории. В Windows для них по умолчанию используется `%LOCALAPPDATA%\Hugin`.
