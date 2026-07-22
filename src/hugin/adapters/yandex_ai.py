from __future__ import annotations

import json
import urllib.error
import urllib.request


class YandexAIError(RuntimeError):
    pass


class YandexAIClient:
    def __init__(
        self,
        api_key: str,
        folder_id: str,
        model: str = "yandexgpt/latest",
        base_url: str = "https://ai.api.cloud.yandex.net/v1",
        timeout_seconds: int = 120,
        temperature: float = 0.1,
    ) -> None:
        self._api_key = api_key.strip()
        self._folder_id = folder_id.strip()
        self._model = model.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._temperature = temperature
        if not self._api_key:
            raise ValueError("Не указан ключ Yandex AI Studio")
        if not self._folder_id:
            raise ValueError("Не указан идентификатор каталога Yandex Cloud")
        if not self._model:
            raise ValueError("Не указана модель YandexGPT")
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("Время ожидания YandexGPT должно быть от 1 до 300 секунд")
        if not 0 <= temperature <= 2:
            raise ValueError("Температура YandexGPT должна быть от 0 до 2")

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        body = {
            "model": self._model_uri(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": True,
            "temperature": self._temperature,
        }
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Api-Key {self._api_key}",
                "x-folder-id": self._folder_id,
                "x-project": self._folder_id,
                "OpenAI-Project": self._folder_id,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-data-logging-enabled": "false",
            },
            method="POST",
        )
        chunks: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if payload == "[DONE]":
                        break
                    chunk = self._parse_chunk(payload)
                    if chunk:
                        chunks.append(chunk)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise YandexAIError(
                f"Yandex AI Studio вернул ошибку HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise YandexAIError(f"Yandex AI Studio недоступен: {error.reason}") from error
        except TimeoutError as error:
            raise YandexAIError("Истекло время ожидания ответа YandexGPT") from error
        except OSError as error:
            raise YandexAIError(f"Ошибка запроса к YandexGPT: {error}") from error

        result = "".join(chunks).strip()
        if not result:
            raise YandexAIError("YandexGPT вернул пустой ответ")
        return result

    def _model_uri(self) -> str:
        if "://" in self._model:
            return self._model
        return f"gpt://{self._folder_id}/{self._model}"

    @staticmethod
    def _parse_chunk(payload: str) -> str:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return ""
        delta = choices[0].get("delta")
        if isinstance(delta, dict):
            return str(delta.get("content") or "")
        message = choices[0].get("message")
        if isinstance(message, dict):
            return str(message.get("content") or "")
        return ""
