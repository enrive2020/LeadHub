"""Обслуживающая команда: доставить задачи лидам, у которых их нет.

    python -m app.pipeline.backfill                  # все активные шаги
    python -m app.pipeline.backfill qualify          # только указанные шаги
    python -m app.pipeline.backfill qualify sheets

Когда пригодится:
  * добавили новый шаг — прогнать через него уже накопленные лиды;
  * лид почему-то остался без задач и молча не обрабатывался;
  * после ручного вмешательства в базу.

Фильтр по шагам — не украшение. Без него прогон нового шага по накопленным
лидам заодно разошлёт пачку уведомлений в Telegram: задачи-то ставятся всем
шагам сразу. Обслуживающая команда не должна ничего делать «за компанию».

Запускать безопасно сколько угодно раз: постановка задач идемпотентна.
"""

import sys

from app.logging_setup import get_logger, setup_logging
from app.pipeline.registry import STEP_NAMES
from app.storage import task_repository
from app.storage.database import init_db

logger = get_logger(__name__)


def main() -> None:
    setup_logging()
    init_db()

    requested = sys.argv[1:] or STEP_NAMES

    unknown = [step for step in requested if step not in STEP_NAMES]
    if unknown:
        logger.error(
            "Неизвестные шаги: %s. Доступны: %s",
            ", ".join(unknown), ", ".join(STEP_NAMES),
        )
        raise SystemExit(1)

    logger.info("Ставлю недостающие задачи для шагов: %s", ", ".join(requested))
    created = task_repository.enqueue_missing(requested)
    if created:
        logger.info("Создано недостающих задач: %s. Воркер их подхватит.", created)
    else:
        logger.info("Все лиды уже обеспечены задачами — делать нечего.")


if __name__ == "__main__":
    main()
