"""Репозиторий лидов — единственное место в проекте, где есть SQL про лиды.

ПАТТЕРН "РЕПОЗИТОРИЙ": прослойка между бизнес-логикой и базой. Наружу торчат
методы на языке предметной области (`save`, `get_by_id`, `list_recent`),
внутри — SQL. Что это даёт:

  * веб-обработчик и воркер не знают, как называются колонки;
  * запросы не размазаны по проекту, их можно оптимизировать в одном месте;
  * в тестах репозиторий подменяется заглушкой в памяти.
"""

import json
import sqlite3
from datetime import datetime
from typing import NamedTuple

from app.domain.lead import Lead, LeadStatus
from app.logging_setup import get_logger
from app.storage import task_repository
from app.storage.database import get_connection

logger = get_logger(__name__)


class SaveResult(NamedTuple):
    """Результат сохранения лида.

    `is_duplicate=True` означает, что такой лид уже был, и в базе ничего не
    изменилось. Вызывающий код обязан это различать: на дубль мы всё равно
    отвечаем 200 OK (иначе отправитель будет ретраить вечно), но повторное
    уведомление владельцу слать не станем.
    """

    lead: Lead
    is_duplicate: bool


def _row_to_lead(row: sqlite3.Row) -> Lead:
    """Превращает строку таблицы обратно в объект Lead."""
    return Lead(
        id=row["id"],
        source=row["source"],
        dedup_key=row["dedup_key"],
        name=row["name"],
        phone=row["phone"],
        email=row["email"],
        message=row["message"],
        raw=json.loads(row["raw_json"]),
        received_at=datetime.fromisoformat(row["received_at"]),
        status=LeadStatus(row["status"]),
    )


def save(lead: Lead, steps: list[str]) -> SaveResult:
    """Сохраняет лид и ставит задачи на его обработку. Одной транзакцией.

    Идемпотентность обеспечена конструкцией `ON CONFLICT ... DO NOTHING`:
    если UNIQUE-индекс по dedup_key нарушен, SQLite молча пропускает вставку
    вместо того, чтобы бросить ошибку.

    Почему не "сначала SELECT, потом INSERT": между двумя этими запросами
    успевает вклиниться параллельный запрос, оба увидят "лида нет" и оба
    вставят. Это состояние гонки. Атомарная проверка на стороне базы —
    единственный надёжный вариант.

    Почему задачи создаются здесь, а не отдельным вызовом после: они должны
    попасть в базу той же транзакцией, что и лид. Иначе процесс, умерший
    между двумя вызовами, оставит лид без единой задачи — он будет лежать
    в базе, и никто никогда его не обработает. Тихая потеря, худший вид.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO leads (
                id, source, dedup_key, name, phone, email, message,
                raw_json, received_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (dedup_key) DO NOTHING
            """,
            (
                lead.id,
                lead.source,
                lead.dedup_key,
                lead.name,
                lead.phone,
                lead.email,
                lead.message,
                # ensure_ascii=False — чтобы русский текст в базе остался
                # читаемым, а не превратился в Иван.
                json.dumps(lead.raw, ensure_ascii=False),
                lead.received_at.isoformat(),
                lead.status.value,
            ),
        )

        # rowcount == 0 значит "конфликт, вставка пропущена" — то есть дубль.
        if cursor.rowcount == 0:
            existing_row = conn.execute(
                "SELECT * FROM leads WHERE dedup_key = ?", (lead.dedup_key,)
            ).fetchone()
            existing = _row_to_lead(existing_row)
            logger.info(
                "Дубль заявки: source=%s, уже сохранён как %s", lead.source, existing.id
            )
            return SaveResult(lead=existing, is_duplicate=True)

        # Та же транзакция: лид и его задачи появляются в базе одновременно.
        task_repository.create_tasks(conn, lead.id, steps)

    logger.info("Принят %s, шагов в очереди: %s", lead.short_repr(), len(steps))
    return SaveResult(lead=lead, is_duplicate=False)


def get_by_id(lead_id: str) -> Lead | None:
    """Возвращает лид по идентификатору или None, если такого нет."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return _row_to_lead(row) if row else None


def list_recent(limit: int = 20) -> list[Lead]:
    """Последние заявки — для отладки и будущей админской ручки."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM leads ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_lead(row) for row in rows]


def count_by_status() -> dict[str, int]:
    """Сводка "сколько лидов в каком состоянии" — основа для health-проверки.

    Если тут копятся записи в статусе failed, значит что-то давно сломано
    и об этом надо узнать раньше, чем позвонит недовольный клиент.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS total FROM leads GROUP BY status"
        ).fetchall()
    return {row["status"]: row["total"] for row in rows}
