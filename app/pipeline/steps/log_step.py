"""Шаг «записать в лог» — временная заглушка.

Нужен, чтобы пайплайн был живым и наблюдаемым до подключения реальных
интеграций. В Фазе 2.2 рядом встанет шаг записи в Google Sheets, в Фазе 3 —
уведомление в Telegram, в Фазе 4 — обращение к LLM.

Заодно это самый короткий пример того, как выглядит шаг: наследник Step,
имя и метод run. Больше от него ничего не требуется.
"""

from app.domain.lead import Lead
from app.logging_setup import get_logger
from app.pipeline.base import Step

logger = get_logger(__name__)


class LogStep(Step):
    name = "log"

    def run(self, lead: Lead) -> None:
        logger.info(
            "[log] лид %s | источник=%s | %s | %s | %s",
            lead.id[:8],
            lead.source,
            lead.name or "без имени",
            lead.phone or lead.email or "без контакта",
            (lead.message or "")[:80],
        )
