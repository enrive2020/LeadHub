"""Воркер — процесс, который доводит лиды до конца.

ПОЧЕМУ ОТДЕЛЬНЫЙ ПРОЦЕСС, А НЕ ФОНОВАЯ ЗАДАЧА ВНУТРИ ВЕБ-СЕРВЕРА.

  * Разные требования. Веб-сервер обязан отвечать за миллисекунды, воркер
    может спокойно ждать ответа LLM четыре секунды. Внутри одного процесса
    они мешали бы друг другу.
  * Изоляция сбоев. Воркер упал на кривом ответе Google — приём заявок
    продолжает работать, лиды копятся в очереди и обработаются после
    перезапуска. Ни одна заявка не потеряна.
  * Независимое масштабирование. Заявок стало много — запускаем три воркера,
    веб-сервер не трогаем. Захват задач написан так, что они не подерутся.
  * Деплой без потерь. Обновляем воркер — приём в это время работает.

Цена — надо запускать две команды вместо одной. Разделение процессов их
не изолирует полностью (общая база), но снимает 90% взаимного влияния,
и это правильный компромисс для проекта такого размера.

Запуск:  python -m app.pipeline.worker
"""

import signal
import threading
from types import FrameType

from app.config import settings
from app.logging_setup import get_logger, setup_logging
from app.pipeline.errors import PermanentError, StepDeferred
from app.pipeline.registry import STEPS_BY_NAME
from app.pipeline.retry import compute_delay
from app.storage import lead_repository, task_repository
from app.storage.database import init_db
from app.storage.task_repository import LeadTask

logger = get_logger(__name__)


class Worker:
    """Цикл: взять готовые задачи -> выполнить -> разобрать результат."""

    def __init__(self) -> None:
        # Event — потокобезопасный флаг "пора останавливаться".
        # Он же используется вместо time.sleep: wait() просыпается либо по
        # таймауту, либо сразу при установке флага. Благодаря этому Ctrl+C
        # не ждёт окончания паузы, а реагирует мгновенно.
        self._stop = threading.Event()

    # -- управление жизненным циклом -------------------------------------

    def request_stop(self) -> None:
        """Просит воркер остановиться после текущей задачи."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Ловит Ctrl+C и сигнал остановки от системы.

        КОРРЕКТНАЯ ОСТАНОВКА (graceful shutdown). По умолчанию сигнал убивает
        процесс мгновенно — в том числе посреди выполнения шага. Задача
        останется в статусе processing, и её придётся подбирать при следующем
        старте.

        Правильное поведение: получили сигнал -> дорабатываем текущую задачу
        -> выходим. При деплое или перезагрузке сервера ни один лид не зависает
        на полпути.

        Второй Ctrl+C убивает процесс жёстко — на случай, если шаг завис.
        """

        def handler(signum: int, _frame: FrameType | None) -> None:
            if self._stop.is_set():
                logger.warning("Повторный сигнал %s — выходим немедленно", signum)
                raise SystemExit(1)
            logger.info("Получен сигнал %s — доработаю текущую задачу и остановлюсь", signum)
            self.request_stop()

        signal.signal(signal.SIGINT, handler)   # Ctrl+C
        signal.signal(signal.SIGTERM, handler)  # docker stop, systemctl stop

    # -- основной цикл ----------------------------------------------------

    def run(self) -> None:
        # Подобрать задачи, зависшие в processing после прошлого падения.
        task_repository.reset_stale_processing()

        logger.info(
            "Воркер запущен. Шаги: %s. Опрос каждые %.1f сек",
            ", ".join(STEPS_BY_NAME) or "нет ни одного",
            settings.worker_poll_interval,
        )

        while not self._stop.is_set():
            handled = self._process_batch()

            # Работы не было — ждём. Была — сразу за следующей порцией,
            # чтобы разгрести накопившееся без искусственных пауз.
            if handled == 0:
                self._stop.wait(settings.worker_poll_interval)

        logger.info("Воркер остановлен")

    def _process_batch(self) -> int:
        """Обрабатывает порцию готовых задач. Возвращает, сколько выполнено."""
        tasks = task_repository.fetch_ready(settings.worker_batch_size)
        handled = 0

        for task in tasks:
            if self._stop.is_set():
                # Останавливаемся между задачами, а не посреди задачи.
                break
            # Не смогли захватить — задачу уже забрал другой воркер.
            if not task_repository.claim(task.id):
                continue
            self._execute(task)
            handled += 1

        return handled

    def _execute(self, task: LeadTask) -> None:
        """Выполняет один шаг и записывает исход в базу."""
        lead = lead_repository.get_by_id(task.lead_id)
        if lead is None:
            # Лида нет — задача осиротела. Повторять нечего.
            task_repository.mark_dead(task.id, task.lead_id, task.attempts, "Лид не найден")
            return

        step = STEPS_BY_NAME.get(task.step)
        if step is None:
            # Шаг есть в базе, но не в коде: его выключили или переименовали.
            # Не хороним задачу — вдруг шаг вернут. Просто откладываем надолго,
            # чтобы не крутиться вхолостую.
            logger.warning("Шаг %r отсутствует в реестре, откладываю задачу", task.step)
            task_repository.schedule_retry(
                task.id, task.attempts, settings.retry_max_delay_seconds,
                f"Шаг {task.step!r} не зарегистрирован",
            )
            return

        attempt = task.attempts + 1

        try:
            step.run(lead)

        except StepDeferred as error:
            # Не сбой: шагу не хватает данных, которые вот-вот появятся.
            # Возвращаем задачу в очередь с ПРЕЖНИМ счётчиком попыток —
            # ожидание не должно расходовать право на повтор.
            task_repository.schedule_retry(task.id, task.attempts, error.delay, str(error))
            logger.info(
                "[%s] лид %s: %s — вернусь через %.0f сек",
                task.step, lead.id[:8], error, error.delay,
            )

        except PermanentError as error:
            # Шаг сам сказал: повтор не поможет. Не тратим попытки.
            logger.error(
                "[%s] лид %s: неустранимая ошибка — %s", task.step, lead.id[:8], error
            )
            task_repository.mark_dead(task.id, lead.id, attempt, str(error))

        except Exception as error:
            # Всё остальное считаем временным. Позиция осторожная: лучше зря
            # повторить, чем зря сдаться и потерять лид.
            self._handle_failure(task, attempt, error)

        else:
            logger.info("[%s] лид %s: успешно", task.step, lead.id[:8])
            task_repository.mark_done(task.id, lead.id)

    def _handle_failure(self, task: LeadTask, attempt: int, error: Exception) -> None:
        """Решает судьбу упавшей задачи: повторить или сдаться."""
        reason = f"{type(error).__name__}: {error}"

        if attempt >= settings.retry_max_attempts:
            logger.error(
                "[%s] лид %s: попытки исчерпаны (%s), уходит в dead letter — %s",
                task.step, task.lead_id[:8], attempt, reason,
            )
            task_repository.mark_dead(task.id, task.lead_id, attempt, reason)
            return

        # Если сервис сам назвал срок — слушаемся его, своя формула тут хуже.
        requested = getattr(error, "retry_after", None)
        delay = float(requested) if requested else compute_delay(attempt)

        next_attempt = task_repository.schedule_retry(task.id, attempt, delay, reason)
        logger.warning(
            "[%s] лид %s: попытка %s/%s не удалась (%s). Повтор через %.1f сек (в %s)%s",
            task.step, task.lead_id[:8], attempt, settings.retry_max_attempts,
            reason, delay, next_attempt.strftime("%H:%M:%S"),
            " — задержку назначил сам сервис" if requested else "",
        )


def main() -> None:
    setup_logging()
    # Воркер может стартовать раньше веб-сервера, поэтому схему готовит сам.
    init_db()

    worker = Worker()
    worker.install_signal_handlers()
    worker.run()


if __name__ == "__main__":
    main()
