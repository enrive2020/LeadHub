"""Репозиторий задач — состояние очереди обработки.

Одна строка = один шаг обработки одного лида. Такая гранулярность и делает
пайплайн честно надёжным: успешный шаг не повторяется из-за упавшего соседа.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.logging_setup import get_logger
from app.storage.database import get_connection

logger = get_logger(__name__)


class TaskStatus:
    """Состояния задачи. Обычные строки — их пишем прямо в SQL."""

    PENDING = "pending"        # ждёт выполнения
    PROCESSING = "processing"  # воркер взял в работу прямо сейчас
    DONE = "done"              # успешно выполнена
    DEAD = "dead"              # попытки исчерпаны — dead letter


@dataclass(frozen=True)
class LeadTask:
    """Задача, готовая к выполнению."""

    id: int
    lead_id: str
    step: str
    attempts: int


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# ---------------------------------------------------------------------------
# Постановка задач
# ---------------------------------------------------------------------------


def create_tasks(conn: sqlite3.Connection, lead_id: str, steps: list[str]) -> None:
    """Ставит задачи для лида. Работает ВНУТРИ чужой транзакции.

    Соединение передаётся аргументом, а не берётся своё, намеренно: задачи
    обязаны создаваться в той же транзакции, что и сам лид. Иначе возможен
    сценарий "лид записан, процесс умер, задачи не созданы" — лид навсегда
    зависнет в базе, и его никто не обработает.

    Либо лид и его задачи, либо ничего.
    """
    now = _iso(_now())
    conn.executemany(
        """
        INSERT INTO lead_tasks (lead_id, step, status, next_attempt_at, updated_at)
        VALUES (?, ?, 'pending', ?, ?)
        -- Повторная постановка тех же задач не создаёт дублей.
        ON CONFLICT (lead_id, step) DO NOTHING
        """,
        [(lead_id, step, now, now) for step in steps],
    )


# ---------------------------------------------------------------------------
# Выборка работы воркером
# ---------------------------------------------------------------------------


def reset_stale_processing() -> int:
    """Возвращает в очередь задачи, зависшие в статусе processing.

    Сценарий: воркер взял задачу, пометил processing и в этот момент его убили
    (перезагрузка сервера, деплой, Ctrl+C). Задача осталась "в работе" навсегда,
    и никто её больше не возьмёт — тихо потерянный лид.

    Поэтому при каждом старте воркер сначала подбирает такие задачи.
    Это ВОССТАНОВЛЕНИЕ ПОСЛЕ СБОЯ, и без него любая надёжность фиктивна:
    сбои случаются именно в самый неудобный момент.
    """
    now = _iso(_now())
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE lead_tasks
               SET status = 'pending', next_attempt_at = ?, updated_at = ?
             WHERE status = 'processing'
            """,
            (now, now),
        )
        recovered = cursor.rowcount

    if recovered:
        logger.warning("Возвращено в очередь после сбоя: %s задач(и)", recovered)
    return recovered


def fetch_ready(limit: int) -> list[LeadTask]:
    """Задачи, которым пора выполняться.

    Условие `next_attempt_at <= сейчас` — и есть механизм отложенного ретрая:
    неудачной задаче ставится время в будущем, и до него она просто невидима.
    Никаких таймеров и sleep в коде — планирование живёт в данных.
    """
    now = _iso(_now())
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, lead_id, step, attempts
              FROM lead_tasks
             WHERE status = 'pending'
               AND next_attempt_at <= ?
             ORDER BY next_attempt_at
             LIMIT ?
            """,
            (now, limit),
        ).fetchall()

    return [
        LeadTask(id=row["id"], lead_id=row["lead_id"], step=row["step"], attempts=row["attempts"])
        for row in rows
    ]


def claim(task_id: int) -> bool:
    """Пытается забрать задачу себе. True — получилось, False — увели.

    Условие `AND status = 'pending'` в UPDATE обязательно. Оно превращает
    "проверить и захватить" в одну атомарную операцию: если задачу уже забрал
    другой воркер, наш UPDATE не затронет ни одной строки, и мы это увидим
    по rowcount.

    Сейчас воркер один и конкуренции нет, но такая блокировка стоит одну строку
    и сразу позволяет запустить несколько воркеров, когда поток вырастет.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE lead_tasks
               SET status = 'processing', updated_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (_iso(_now()), task_id),
        )
        return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# Завершение задач
# ---------------------------------------------------------------------------


def mark_done(task_id: int, lead_id: str) -> None:
    """Шаг выполнен успешно."""
    now = _iso(_now())
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE lead_tasks
               SET status = 'done', last_error = NULL, updated_at = ?
             WHERE id = ?
            """,
            (now, task_id),
        )
        _refresh_lead_status(conn, lead_id)


def schedule_retry(task_id: int, attempts: int, delay_seconds: float, error: str) -> datetime:
    """Откладывает повторную попытку и возвращает её время."""
    now = _now()
    next_attempt = now + timedelta(seconds=delay_seconds)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE lead_tasks
               SET status = 'pending',
                   attempts = ?,
                   last_error = ?,
                   next_attempt_at = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (attempts, error[:2000], _iso(next_attempt), _iso(now), task_id),
        )
    return next_attempt


def mark_dead(task_id: int, lead_id: str, attempts: int, error: str) -> None:
    """Переводит задачу в dead letter — «сдались, нужен человек».

    DEAD LETTER — это не корзина, а полка. Задача остаётся в базе со всей
    историей: сколько было попыток и с какой ошибкой упала последняя.
    Её видно в /healthz, можно разобрать причину и перезапустить руками.

    Разница принципиальная. Когда сдаётся связка на n8n, заявка просто исчезает
    из потока, и узнаёшь ты об этом от клиента, который "звонил, а вы не
    перезвонили". Здесь потеря становится ВИДИМОЙ.
    """
    now = _iso(_now())
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE lead_tasks
               SET status = 'dead', attempts = ?, last_error = ?, updated_at = ?
             WHERE id = ?
            """,
            (attempts, error[:2000], now, task_id),
        )
        _refresh_lead_status(conn, lead_id)


def _refresh_lead_status(conn: sqlite3.Connection, lead_id: str) -> None:
    """Пересчитывает статус лида по состоянию его задач.

    Статус лида — производная величина, а не отдельная правда. Считаем его
    из задач, чтобы он не мог разъехаться с реальностью.

        есть незавершённые   -> processing
        все done             -> done
        остальное (есть dead)-> failed
    """
    counts = {
        row["status"]: row["total"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS total FROM lead_tasks WHERE lead_id = ? GROUP BY status",
            (lead_id,),
        ).fetchall()
    }

    if counts.get(TaskStatus.PENDING) or counts.get(TaskStatus.PROCESSING):
        lead_status = "processing"
    elif counts.get(TaskStatus.DEAD):
        lead_status = "failed"
    else:
        lead_status = "done"

    conn.execute("UPDATE leads SET status = ? WHERE id = ?", (lead_status, lead_id))


# ---------------------------------------------------------------------------
# Наблюдаемость
# ---------------------------------------------------------------------------


def stats() -> dict[str, dict[str, int]]:
    """Сводка по шагам и статусам — для /healthz.

    Отвечает на вопрос "всё ли хорошо" без захода в базу руками.
    Ненулевой dead — сигнал, что пора смотреть логи.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT step, status, COUNT(*) AS total FROM lead_tasks GROUP BY step, status"
        ).fetchall()

    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(row["step"], {})[row["status"]] = row["total"]
    return result


def enqueue_missing(steps: list[str]) -> int:
    """Ставит задачи лидам, у которых их не хватает. Возвращает число созданных.

    Зачем это нужно. Лид может остаться без задачи по нескольким причинам:
      * он появился раньше, чем шаг был добавлен в реестр;
      * шаг добавили в работающую систему, и старым лидам он не достался;
      * что-то пошло не так при постановке.

    Такой лид лежит в базе и молча не обрабатывается — самая обидная потеря,
    потому что данные-то есть. Эта команда его чинит.

    Безопасна к повторному запуску: постановка задач идемпотентна
    (UNIQUE (lead_id, step) + ON CONFLICT DO NOTHING).
    """
    now = _iso(_now())
    with get_connection() as conn:
        lead_ids = [row["id"] for row in conn.execute("SELECT id FROM leads").fetchall()]
        cursor = conn.executemany(
            """
            INSERT INTO lead_tasks (lead_id, step, status, next_attempt_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            ON CONFLICT (lead_id, step) DO NOTHING
            """,
            [(lead_id, step, now, now) for lead_id in lead_ids for step in steps],
        )
        created = cursor.rowcount

        # Лиды, которым досталась работа, снова считаются незавершёнными.
        if created:
            conn.execute(
                """
                UPDATE leads SET status = 'processing'
                 WHERE id IN (SELECT lead_id FROM lead_tasks WHERE status = 'pending')
                """
            )

    return created


def requeue_dead(step: str | None = None) -> int:
    """Возвращает похороненные задачи в очередь. Возвращает число возвращённых.

    ЗАЧЕМ. Задача уходит в dead letter, когда повторять бессмысленно: нет
    доступа к таблице, боту не написали первым, неверный ключ. Причина при этом
    почти всегда УСТРАНИМА — человек чинит настройку за минуту. Но без этой
    команды исправленная причина ничего не меняет: лиды так и лежат мёртвыми.

    Dead letter имеет смысл только в паре с кнопкой "попробовать снова".
    Иначе это не полка, а всё та же корзина, только с красивым названием.

    Счётчик попыток обнуляем: причина другая, история старых попыток
    к новым отношения не имеет.
    """
    now = _iso(_now())
    with get_connection() as conn:
        if step:
            cursor = conn.execute(
                """
                UPDATE lead_tasks
                   SET status = 'pending', attempts = 0,
                       next_attempt_at = ?, updated_at = ?
                 WHERE status = 'dead' AND step = ?
                """,
                (now, now, step),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE lead_tasks
                   SET status = 'pending', attempts = 0,
                       next_attempt_at = ?, updated_at = ?
                 WHERE status = 'dead'
                """,
                (now, now),
            )
        requeued = cursor.rowcount

        # Лиды, у которых снова появилась работа, больше не "failed".
        if requeued:
            conn.execute(
                """
                UPDATE leads SET status = 'processing'
                 WHERE id IN (SELECT lead_id FROM lead_tasks WHERE status = 'pending')
                """
            )

    return requeued


def list_dead() -> list[dict[str, object]]:
    """Задачи, требующие ручного разбора."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.lead_id, t.step, t.attempts, t.last_error, t.updated_at,
                   l.name, l.phone, l.email
              FROM lead_tasks t
              JOIN leads l ON l.id = t.lead_id
             WHERE t.status = 'dead'
             ORDER BY t.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
