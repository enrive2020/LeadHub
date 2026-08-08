"""Шаг: оценить лид с помощью LLM и сохранить результат.

Тонкая прослойка между пайплайном и AI-слоем. Вся её работа — вызвать
квалификатор и перевести его ошибки на язык, понятный воркеру.
"""

from app.ai.qualifier import Qualifier
from app.domain.lead import Lead
from app.llm.errors import LLMBadOutput, LLMRejected, LLMUnavailable
from app.llm.factory import build_provider
from app.logging_setup import get_logger
from app.pipeline.base import Step
from app.pipeline.errors import PermanentError, RetryableError
from app.storage import ai_repository

logger = get_logger(__name__)


class QualifyStep(Step):
    name = "qualify"

    def __init__(self) -> None:
        self._qualifier = Qualifier(build_provider())

    def run(self, lead: Lead) -> None:
        try:
            qualification = self._qualifier.qualify(lead)

        except LLMUnavailable as error:
            # Сервис недоступен или упёрлись в лимит — подождём и повторим.
            raise RetryableError(
                f"Модель недоступна: {error}", retry_after=error.retry_after
            ) from error

        except LLMRejected as error:
            # Неверный ключ, нет доступа к модели, кончились деньги.
            # Повторять нечего, и каждая попытка — потраченные деньги.
            raise PermanentError(f"Провайдер LLM отказал: {error}") from error

        except LLMBadOutput as error:
            # Модель отвечала, но не смогла выдержать формат даже после
            # просьбы исправиться.
            #
            # Считаем ВРЕМЕННОЙ ошибкой, хотя соблазн назвать её постоянной
            # велик. Причина: ответы модели недетерминированы, и следующая
            # попытка через минуту вполне может пройти — в отличие от
            # неверного ключа, где ничего не изменится никогда.
            # Число попыток ограничено настройками, так что бесконечно
            # платить за неудачи мы не будем.
            raise RetryableError(f"Модель не выдержала формат ответа: {error}") from error

        ai_repository.save(lead.id, qualification, model=self._qualifier.model_name)

        logger.info(
            "Лид %s оценён: %s (%s/100) — %s",
            lead.id[:8],
            qualification.grade.value,
            qualification.score,
            qualification.reason,
        )
