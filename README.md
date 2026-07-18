# Hugin

Hugin — местный персональный агент поиска работы. Сейчас создана основа серверной части: настройки, фабрика приложения FastAPI и проверка состояния `/health`.

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

При запуске схема SQLite автоматически обновляется до текущей версии. База хранится в каталоге данных Hugin: `%LOCALAPPDATA%\Hugin\hugin.db` в Windows и `/data/hugin.db` в контейнере.

Ручное управление схемой:

```powershell
uv run hugin-db upgrade
uv run hugin-db current
uv run hugin-db downgrade
```

## Запуск в Docker

```powershell
docker compose up -d --build --wait
Invoke-RestMethod http://127.0.0.1:8010/health
```

Контейнер запускает только серверную часть: порт `8010` компьютера направляется на порт `8000` контейнера и доступен исключительно через `127.0.0.1`. Рабочие данные хранятся в отдельном томе `hugin-data`. Видимый браузер и возможности Windows в контейнер не переносятся.

Остановка без удаления рабочих данных:

```powershell
docker compose down
```

## Проверки

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=hugin --cov-report=term-missing
```

Рабочая база, журналы, профиль браузера, снимки и трассировки не должны находиться в репозитории. В Windows для них по умолчанию используется `%LOCALAPPDATA%\Hugin`.
