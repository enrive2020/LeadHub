"""Обслуживающая команда: доставить задачи лидам, у которых их нет.

    python -m app.pipeline.backfill

Когда пригодится:
  * добавили новый шаг — прогнать через него уже накопленные лиды;
  * лид почему-то остался без задач и молча не обрабатывался;
  * после ручного вмешательства в базу.

Запускать безопасно сколько угодно раз: постановка задач идемпотентна.
"""

from app.logging_setup import get_logger, setup_logging
from app.pipeline.registry import STEP_NAMES
from app.storage import task_repository
from app.storage.database import init_db

logger = get_logger(__name__)


def main() -> None:
    setup_logging()
    init_db()

    created = task_repository.enqueue_missing(STEP_NAMES)
    if created:
        logger.info("Создано недостающих задач: %s. Воркер их подхватит.", created)
    else:
        logger.info("Все лиды уже обеспечены задачами — делать нечего.")


if __name__ == "__main__":
    main()
