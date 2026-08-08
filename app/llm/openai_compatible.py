"""Провайдер для любого API, говорящего на протоколе OpenAI Chat Completions.

ПОЧЕМУ ОДИН КЛАСС ПОДХОДИТ К ДЕСЯТКУ СЕРВИСОВ.

OpenAI первой выпустила популярный API для чат-моделей, и её формат стал
де-факто стандартом. Теперь почти все реализуют такой же эндпоинт, чтобы
разработчикам не пришлось ничего переписывать при переходе. В итоге один
и тот же код работает с:

    Google Gemini    https://generativelanguage.googleapis.com/v1beta/openai/
    OpenAI           https://api.openai.com/v1/
    OpenRouter       https://openrouter.ai/api/v1/
    Ollama (локально) http://localhost:11434/v1/
    Anthropic        https://api.anthropic.com/v1/   (см. оговорку ниже)

Меняются только BASE_URL, ключ и имя модели — всё это в .env.

ОГОВОРКА ПРО CLAUDE. Слой совместимости у Anthropic существует, но заметная
часть параметров там ИГНОРИРУЕТСЯ МОЛЧА — в том числе response_format, то есть
именно механизм структурированного вывода. Anthropic прямо пишет, что этот слой
предназначен для проб и сравнения моделей, а не для эксплуатации. Понадобится
Claude всерьёз — правильный ход не «подставить base_url», а написать
AnthropicProvider на нативном SDK. Наш слой llm/ ровно для этого и сделан:
это будет новый файл и строка в фабрике.

ПОЧЕМУ ГОЛЫЙ requests, А НЕ БИБЛИОТЕКА openai. Запрос к модели — обычный
HTTP-вызов, и полезно видеть его целиком: какие поля уходят, что приходит,
где именно случаются ошибки. Плюс странно тащить пакет с именем openai ради
общения с Gemini. Если понадобятся стриминг или tool calling — родной SDK
станет оправдан.
"""

from typing import Any

import requests
from requests.exceptions import RequestException

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.errors import LLMRejected, LLMUnavailable
from app.logging_setup import get_logger

logger = get_logger(__name__)

# Таймаут запроса, сек. Модель думает дольше обычного API, поэтому щедрее,
# чем у Telegram, — но конечный. Бесконечное ожидание останавливает воркер
# навсегда и не видно в логах.
REQUEST_TIMEOUT = 60

# Коды, при которых повторять бессмысленно: проблема в настройке или в запросе.
#   400 — некорректный запрос      401 — ключ неверен
#   403 — нет доступа к модели     404 — модели с таким именем нет
_PERMANENT_STATUSES = {400, 401, 403, 404}

# Временные: перегрузка, лимиты, сбой на стороне провайдера.
_RETRYABLE_STATUSES = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider(LLMProvider):
    """Клиент к чат-модели по протоколу OpenAI Chat Completions."""

    def __init__(self) -> None:
        if not settings.llm_api_key:
            raise LLMRejected("Не задан LLM_API_KEY")
        if not settings.llm_base_url:
            raise LLMRejected("Не задан LLM_BASE_URL")
        if not settings.llm_model:
            raise LLMRejected("Не задан LLM_MODEL")

        # Имя модели попадает в лог и сохраняется рядом с оценкой: через месяц
        # надо понимать, чем именно был оценён конкретный лид.
        self.name = settings.llm_model

        # Переиспользуем TCP-соединение между запросами вместо нового
        # TLS-рукопожатия на каждый лид.
        self._session = requests.Session()
        self._session.headers.update(
            {
                # Bearer-токен — стандартная схема этого протокола.
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            }
        )
        self._url = settings.llm_base_url.rstrip("/") + "/chat/completions"

    # -- запрос -----------------------------------------------------------

    def complete(
        self, *, system: str, user: str, temperature: float, max_tokens: int
    ) -> str:
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            # Вот он, весь «диалог» с моделью. Роли разделены намеренно:
            # system — наши инструкции, user — данные постороннего человека.
            # Модель не помнит ничего между запросами, поэтому весь нужный
            # контекст уходит здесь целиком, каждый раз заново.
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if settings.llm_json_mode:
            # JSON-режим: провайдер сам следит, чтобы ответ был валидным JSON.
            # Снимает целый класс сбоев — markdown-обёртки и вежливые
            # вступления просто перестают появляться.
            #
            # НО ЭТО НЕ ОТМЕНЯЕТ НАШУ ВАЛИДАЦИЮ. Гарантируется синтаксис
            # («это JSON»), а не смысл («есть поле grade с допустимым
            # значением»). Плюс поддержка у провайдеров разная, и часть из них
            # игнорирует параметр МОЛЧА, возвращая 200 OK. Проверять ответ
            # схемой всё равно обязаны мы сами.
            payload["response_format"] = {"type": "json_object"}

        try:
            response = self._session.post(
                self._url, json=payload, timeout=REQUEST_TIMEOUT
            )
        except RequestException as error:
            raise LLMUnavailable(f"Не достучались до провайдера LLM: {error}") from error

        if response.status_code != 200:
            self._raise_for_status(response)

        return self._extract_text(response)

    # -- разбор ответа ----------------------------------------------------

    def _extract_text(self, response: requests.Response) -> str:
        """Достаёт текст ответа и пишет в лог стоимость запроса."""
        try:
            data = response.json()
        except ValueError as error:
            raise LLMUnavailable("Провайдер вернул не-JSON") from error

        # Учёт токенов. Деньги за LLM утекают незаметно: каждый запрос стоит
        # копейки, но на потоке заявок счёт складывается. Если этого нет
        # в логах, узнаёшь о расходе только из счёта в конце месяца.
        usage = data.get("usage") or {}
        logger.info(
            "LLM %s: токенов на входе %s, на выходе %s",
            settings.llm_model,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )

        try:
            choices = data["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMUnavailable(f"Неожиданная структура ответа: {data}") from error

        if not content:
            # Пустой ответ бывает при срабатывании фильтров безопасности
            # провайдера. Считаем временным: следующая формулировка может пройти.
            finish_reason = (choices[0] or {}).get("finish_reason")
            raise LLMUnavailable(f"Модель вернула пустой ответ (finish_reason={finish_reason})")

        return content

    def _raise_for_status(self, response: requests.Response) -> None:
        """Переводит HTTP-ошибку провайдера в понятный воркеру класс."""
        status = response.status_code

        # Текст ошибки полезен для лога, но может быть огромным — режем.
        detail = response.text[:300]

        if status == 429:
            # Лимит запросов. Многие провайдеры сами говорят, сколько ждать —
            # уважаем их значение вместо своей формулы.
            retry_after = response.headers.get("retry-after")
            raise LLMUnavailable(
                f"Превышен лимит запросов к модели: {detail}",
                retry_after=float(retry_after) if retry_after else None,
            )

        if status in _PERMANENT_STATUSES:
            raise LLMRejected(f"Провайдер отказал (HTTP {status}): {detail}")

        if status in _RETRYABLE_STATUSES:
            raise LLMUnavailable(f"Провайдер временно недоступен (HTTP {status}): {detail}")

        # Незнакомый код — считаем временным: лучше зря повторить.
        raise LLMUnavailable(f"Неизвестная ошибка провайдера (HTTP {status}): {detail}")
