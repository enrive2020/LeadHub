"""Подключение к SQLite и схема базы.

ПОЧЕМУ SQLITE. Нам нужна очередь, переживающая перезапуск процесса и падение
сервера. Варианты были такие:

  * Redis / RabbitMQ — правильные очереди для больших нагрузок, но это отдельный
    сервис, который надо поднять, настроить и мониторить. Для потока в десятки
    заявок в день это стрельба из пушки по воробьям, а заказчику — лишние
    расходы на хостинг.
  * PostgreSQL — тоже требует отдельный сервер.
  * Файл JSON — нет транзакций. Процесс упал посередине записи — файл битый,
    все лиды потеряны. Ровно то, от чего мы уходим.
  * SQLite — полноценная СУБД с транзакциями, живущая в одном файле рядом с
    кодом. Ноль настройки, входит в стандартную библиотеку Python.

SQLite спокойно держит тысячи записей в секунду — наш поток он не заметит.
А слой репозитория написан так, что переезд на PostgreSQL, когда он реально
понадобится, затронет только этот пакет.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import settings
from app.logging_setup import get_logger

logger = get_logger(__name__)


# Схема базы. Пишем на чистом SQL, без ORM: запросов у нас мало, они простые,
# а видеть точную структуру таблицы полезнее, чем прятать её за абстракцией.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              TEXT PRIMARY KEY,

    -- Источник как ДАННЫЕ, а не как ветка кода. Новый канал = новое значение
    -- в этой колонке, схему менять не нужно.
    source          TEXT NOT NULL,

    -- UNIQUE — и есть техническая реализация идемпотентности приёма.
    -- Повторную заявку отсекает сама база, а не наш код: это единственный
    -- способ не словить гонку при одновременных запросах.
    dedup_key       TEXT NOT NULL UNIQUE,

    name            TEXT,
    phone           TEXT,
    email           TEXT,
    message         TEXT,

    -- Исходный payload целиком, в JSON. SQLite не умеет тип "словарь",
    -- поэтому сериализуем в текст.
    raw_json        TEXT NOT NULL,

    -- Даты храним строками ISO-8601 в UTC ("2026-08-08T14:30:00+00:00").
    -- Так они сортируются лексикографически = сортируются хронологически,
    -- и не зависят от часового пояса машины, где крутится сервис.
    received_at     TEXT NOT NULL,

    status          TEXT NOT NULL DEFAULT 'new',

    -- Поля для воркера. Заполняются начиная с Фазы 2, заводим сейчас,
    -- чтобы не менять схему на живой базе.
    attempts        INTEGER NOT NULL DEFAULT 0,   -- сколько раз пробовали обработать
    last_error      TEXT,                          -- текст последней ошибки
    next_attempt_at TEXT                           -- когда пробовать снова (ретрай)
);

-- Индекс под главный запрос воркера: "дай лиды, готовые к обработке".
-- Без него база при каждом опросе читает всю таблицу целиком.
CREATE INDEX IF NOT EXISTS idx_leads_status
    ON leads (status, next_attempt_at);

-- Индекс под просмотр последних заявок.
CREATE INDEX IF NOT EXISTS idx_leads_received_at
    ON leads (received_at DESC);
"""


def _configure(conn: sqlite3.Connection) -> None:
    """Настройки соединения. Применяются к каждому новому подключению."""
    # Результат запроса — объект с доступом по имени колонки (row["email"]),
    # а не безымянный кортеж (row[4]). Код перестаёт ломаться при добавлении
    # колонки в середину таблицы.
    conn.row_factory = sqlite3.Row

    # WAL (Write-Ahead Logging) — режим журналирования, при котором записи
    # сначала попадают в отдельный лог-файл. Даёт две важные для нас вещи:
    #   1. Читатели не блокируют писателя. Веб-сервер принимает новый лид,
    #      пока воркер читает базу, — они не ждут друг друга.
    #   2. При аварийном завершении процесса база восстанавливается из журнала,
    #      а не остаётся битой.
    conn.execute("PRAGMA journal_mode=WAL")

    # Если база занята другим процессом — подождать до 5 секунд, а не падать
    # сразу с "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Соединение с базой в виде контекстного менеджера.

        with get_connection() as conn:
            ...

    Что происходит на выходе из блока:
      * код отработал без исключения -> COMMIT (изменения сохранены);
      * вылетело исключение          -> ROLLBACK (база вернулась как было);
      * в любом случае               -> соединение закрыто.

    Это ТРАНЗАКЦИЯ — либо применяются все изменения внутри блока, либо ни одно.
    Именно она не даёт получить полузаписанный лид, если процесс умрёт посреди
    операции. Ради этого мы и берём настоящую СУБД вместо файла.
    """
    conn = sqlite3.connect(settings.db_path_absolute)
    _configure(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Создаёт файл базы и таблицы, если их ещё нет.

    Безопасно вызывать сколько угодно раз — все выражения написаны
    с IF NOT EXISTS. Вызывается при старте приложения.
    """
    db_path = settings.db_path_absolute
    # data/ лежит в .gitignore, поэтому в свежем клоне репозитория папки нет.
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        conn.executescript(_SCHEMA)

    logger.info("База готова: %s", db_path)


if __name__ == "__main__":
    # Ручная инициализация: py -m app.storage.database
    from app.logging_setup import setup_logging

    setup_logging()
    init_db()
