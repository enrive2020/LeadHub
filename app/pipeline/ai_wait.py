"""Мягкое ожидание оценки AI — общее для шагов, которые её показывают.

Оценка считается параллельно с доставкой, и к моменту записи в таблицу или
отправки уведомления её может ещё не быть. Здесь описано, как правильно себя
при этом вести.

ПРАВИЛО: важное не блокируется необязательным, но по возможности им обогащается.

  * оценка готова          -> отдаём её, карточка полная;
  * оценка ещё считается   -> ждём, но не бесконечно;
  * оценки не будет или
    терпение кончилось     -> работаем без неё, ничего не теряя.

Жёсткая зависимость («не уведомлять, пока нет оценки») выглядит аккуратнее,
но означает, что при недоступном провайдере LLM владелец не узнает о заявке
ВООБЩЕ. Отказ второстепенной функции обязан деградировать, а не отменять
основную.
"""

from datetime import UTC, datetime

from app.ai.schemas import Qualification
from app.config import settings
from app.domain.lead import Lead
from app.logging_setup import get_logger
from app.pipeline.errors import StepDeferred
from app.storage import ai_repository, task_repository
from app.storage.task_repository import TaskStatus

logger = get_logger(__name__)

QUALIFY_STEP = "qualify"

# Пауза между проверками. Меньше — быстрее реакция, больше — меньше запросов
# к базе. Оценка обычно готова за 3–6 секунд, так что 5 попадает в такт.
_RECHECK_DELAY_SECONDS = 5.0


def qualification_or_none(lead: Lead) -> Qualification | None:
    """Оценка лида, если она есть или вот-вот будет.

    Возвращает None, когда оценки не будет вовсе или ждать больше нельзя —
    вызывающий шаг обязан отработать и без неё.

    Бросает StepDeferred, если оценка ещё считается и время терпения не вышло.
    """
    qualification = ai_repository.get(lead.id)
    if qualification is not None:
        return qualification

    status = task_repository.get_status(lead.id, QUALIFY_STEP)

    # Шага оценки нет вовсе (AI отключён) или он уже завершился — ждать нечего.
    if status is None or status in (TaskStatus.DONE, TaskStatus.DEAD):
        if status == TaskStatus.DEAD:
            logger.warning("Лид %s: оценка не удалась, работаю без неё", lead.id[:8])
        return None

    # Ограничитель ожидания считаем от момента ПРИЁМА заявки, а не от начала
    # шага. Так порог означает понятную вещь: "не тянуть с уведомлением дольше
    # N секунд с момента, когда клиент нажал кнопку". И для этого не понадобилось
    # ни новой колонки, ни счётчика — время приёма у лида уже есть.
    waited = (datetime.now(UTC) - lead.received_at).total_seconds()

    if waited >= settings.ai_wait_seconds:
        logger.warning(
            "Лид %s: оценка не готова за %.0f сек — не задерживаю доставку",
            lead.id[:8], waited,
        )
        return None

    raise StepDeferred("жду оценку AI", delay=_RECHECK_DELAY_SECONDS)
