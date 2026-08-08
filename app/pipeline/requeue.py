"""Обслуживающая команда: вернуть похороненные задачи в очередь.

    python -m app.pipeline.requeue            # все задачи из dead letter
    python -m app.pipeline.requeue telegram   # только конкретный шаг

Типичный сценарий: увидел в /healthz ненулевой dead -> посмотрел причину ->
починил настройку (расшарил таблицу, написал боту, обновил ключ) -> запустил
эту команду -> воркер доводит лиды до конца.

Без такой команды dead letter бесполезен: причина устранена, а лиды всё равно
лежат мёртвыми. Полка нужна вместе с возможностью снять с неё вещь обратно.
"""

import sys

from app.logging_setup import get_logger, setup_logging
from app.storage import task_repository
from app.storage.database import init_db

logger = get_logger(__name__)


def main() -> None:
    setup_logging()
    init_db()

    step = sys.argv[1] if len(sys.argv) > 1 else None

    dead = task_repository.list_dead()
    if not dead:
        logger.info("В dead letter пусто — возвращать нечего.")
        return

    logger.info("Сейчас в dead letter: %s задач(и)", len(dead))
    for item in dead:
        logger.info(
            "  лид %s | шаг %s | попыток %s | %s",
            str(item["lead_id"])[:8], item["step"], item["attempts"], item["last_error"],
        )

    requeued = task_repository.requeue_dead(step)
    logger.info(
        "Возвращено в очередь: %s задач(и)%s. Воркер их подхватит.",
        requeued,
        f" (шаг {step})" if step else "",
    )


if __name__ == "__main__":
    main()
