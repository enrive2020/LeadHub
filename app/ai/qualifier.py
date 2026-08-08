"""Квалификация лида: собираем всё вместе.

Цикл здесь простой, но именно он превращает «дёрнул нейросеть» в
предсказуемый результат:

    спросить -> разобрать -> не вышло? объяснить, что не так, и переспросить
             -> опять не вышло? честно сдаться, лид не потерян
"""

from app.ai.parsing import parse_qualification
from app.ai.prompts import SYSTEM_PROMPT, render_lead, render_repair_request
from app.ai.schemas import Qualification
from app.config import settings
from app.domain.lead import Lead
from app.llm.base import LLMProvider
from app.llm.errors import LLMBadOutput
from app.logging_setup import get_logger

logger = get_logger(__name__)


class Qualifier:
    """Оценивает лид с помощью языковой модели."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    @property
    def model_name(self) -> str:
        return self._provider.name

    def qualify(self, lead: Lead) -> Qualification:
        """Возвращает проверенную оценку заявки.

        Бросает LLMBadOutput, если модель так и не смогла ответить по формату,
        и LLMUnavailable / LLMRejected — если не отработал сам сервис.
        Разбираться с этим будет шаг пайплайна: у него есть ретраи и dead letter.
        """
        base_user_message = render_lead(
            source=lead.source,
            name=lead.name,
            phone=lead.phone,
            email=lead.email,
            message=lead.message,
        )

        user_message = base_user_message
        last_problem: str | None = None

        for attempt in range(1, settings.llm_max_parse_attempts + 1):
            raw = self._provider.complete(
                system=SYSTEM_PROMPT,
                user=user_message,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
            )

            try:
                qualification = parse_qualification(raw)
            except LLMBadOutput as error:
                last_problem = str(error)
                logger.warning(
                    "Лид %s: попытка разбора %s/%s не удалась — %s",
                    lead.id[:8], attempt, settings.llm_max_parse_attempts, last_problem,
                )

                # Следующий запрос идёт с претензией. Исходные данные заявки
                # оставляем: без них модели не из чего собирать ответ заново.
                user_message = (
                    base_user_message
                    + "\n\n---\n\n"
                    + render_repair_request(previous_answer=raw, problem=last_problem)
                )
                continue

            if attempt > 1:
                logger.info("Лид %s: модель исправилась с %s-й попытки", lead.id[:8], attempt)
            return qualification

        # Попытки исчерпаны. Бросаем осознанно, а не возвращаем "пустую" оценку:
        # выдуманный результат хуже отсутствующего — на него будут смотреть
        # как на настоящий и принимать по нему решения.
        raise LLMBadOutput(
            f"Модель не вернула корректный ответ за {settings.llm_max_parse_attempts} "
            f"попыт(ки). Последняя проблема: {last_problem}"
        )
