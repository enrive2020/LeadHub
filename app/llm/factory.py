"""Выбор провайдера по настройкам.

Единственное место, где принимается решение «какая модель работает».
Весь остальной код получает готовый объект и не знает, кто за ним стоит.
"""

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.fake import FakeProvider
from app.logging_setup import get_logger

logger = get_logger(__name__)


def build_provider() -> LLMProvider:
    """Создаёт провайдера согласно LLM_PROVIDER в .env."""
    provider_name = settings.llm_provider

    if provider_name == "fake":
        logger.info("LLM: заглушка (реальные запросы не отправляются)")
        return FakeProvider()

    if provider_name == "openai_compatible":
        # Импорт внутри функции намеренно: этот провайдер появится в шаге 4.2.
        # Так проект остаётся запускаемым, даже когда его ещё нет.
        from app.llm.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider()

    raise ValueError(f"Неизвестный LLM_PROVIDER: {provider_name!r}")
