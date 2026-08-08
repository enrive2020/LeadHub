"""Подключение к SQLite и версионированная схема базы.

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


# ---------------------------------------------------------------------------
# МИГРАЦИИ
#
# Схема базы меняется по ходу проекта, а база у клиента уже работает с живыми
# данными — снести и создать заново нельзя. Миграция — это пронумерованный шаг
# изменения схемы. База помнит свой номер, при старте применяются только те
# шаги, которых ей не хватает.
#
# Номер храним в PRAGMA user_version — встроенное в файл SQLite целое число,
# специально оставленное под нужды приложения. Отдельная таблица не нужна.
#
# ПРАВИЛО 1. Применённую миграцию НИКОГДА не редактируют: у кого база уже
# обновилась, тот правку не увидит, и схемы разъедутся. Нужна поправка —
# добавляй следующий номер.
#
# ПРАВИЛО 2. Миграция обязана быть атомарной: применилась целиком или никак.
# Поэтому BEGIN/COMMIT и установка номера версии живут ВНУТРИ текста скрипта.
# Снаружи обернуть не выйдет — executescript() сначала делает COMMIT текущей
# транзакции, и внешний блок на него не действует. Мы на этом обожглись:
# миграция #2 упала посередине, часть изменений применилась, номер версии
# остался старым, и база оказалась в состоянии, которого нет ни в одной версии
# схемы. Ровно тот случай, когда "у меня работает" превращается в ночной вызов.
#
# ПРАВИЛО 3. Никаких комментариев внутри CREATE TABLE. SQLite хранит текст
# объявления дословно и при ALTER TABLE переразбирает его — комментарии ломают
# разбор ("incomplete input"). Пояснения пишем над оператором.
# ---------------------------------------------------------------------------

# --- Миграция 1: таблица лидов -------------------------------------------
#   source      — источник как ДАННЫЕ, а не ветка кода: новый канал = новое
#                 значение в колонке, схему менять не нужно;
#   dedup_key   — UNIQUE здесь и есть техническая реализация идемпотентности
#                 приёма: повтор отсекает сама база, а не наш код (иначе гонка);
#   raw_json    — исходный payload целиком, SQLite не умеет тип "словарь";
#   received_at — строка ISO-8601 в UTC: сортируется лексикографически =
#                 хронологически и не зависит от часового пояса машины.
_MIGRATION_1_LEADS = """
BEGIN;

CREATE TABLE IF NOT EXISTS leads (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    dedup_key       TEXT NOT NULL UNIQUE,
    name            TEXT,
    phone           TEXT,
    email           TEXT,
    message         TEXT,
    raw_json        TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_leads_received_at ON leads (received_at DESC);

PRAGMA user_version = 1;
COMMIT;
"""

# --- Миграция 2: очередь задач -------------------------------------------
# Счётчики попыток переезжают с лида на отдельные задачи: у каждого шага своя
# судьба. Sheets записался, а Telegram упал — повторяем ТОЛЬКО Telegram, иначе
# получим дубль строки в таблице.
#
# Лишние колонки убираем не через ALTER TABLE DROP COLUMN, а пересозданием
# таблицы. Причина — грабли из ПРАВИЛА 3 выше; заодно этот способ приводит
# в порядок базу, пострадавшую от неудачной попытки миграции.
#
# lead_tasks: одна строка = один шаг обработки одного лида.
#   step            — имя шага ("sheets", "telegram", "llm"): снова данные,
#                     а не код — новый шаг не требует менять схему;
#   status          — pending | processing | done | dead;
#   next_attempt_at — момент, раньше которого задачу брать нельзя; так
#                     реализована отложенная повторная попытка;
#   UNIQUE(lead_id, step) — идемпотентность постановки задач.
_MIGRATION_2_TASKS = """
BEGIN;

CREATE TABLE leads_rebuilt (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    dedup_key       TEXT NOT NULL UNIQUE,
    name            TEXT,
    phone           TEXT,
    email           TEXT,
    message         TEXT,
    raw_json        TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new'
);

INSERT INTO leads_rebuilt
    (id, source, dedup_key, name, phone, email, message, raw_json, received_at, status)
SELECT
     id, source, dedup_key, name, phone, email, message, raw_json, received_at, status
  FROM leads;

DROP TABLE leads;
ALTER TABLE leads_rebuilt RENAME TO leads;

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status);
CREATE INDEX IF NOT EXISTS idx_leads_received_at ON leads (received_at DESC);

CREATE TABLE IF NOT EXISTS lead_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         TEXT NOT NULL REFERENCES leads (id) ON DELETE CASCADE,
    step            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (lead_id, step)
);

CREATE INDEX IF NOT EXISTS idx_tasks_ready ON lead_tasks (status, next_attempt_at);

PRAGMA user_version = 2;
COMMIT;
"""

# Список миграций по порядку. Добавляя новую, дописывай в конец.
_MIGRATIONS: list[tuple[int, str]] = [
    (1, _MIGRATION_1_LEADS),
    (2, _MIGRATION_2_TASKS),
]


def _configure(conn: sqlite3.Connection) -> None:
    """Настройки соединения. Применяются к каждому новому подключению."""
    # Результат запроса — объект с доступом по имени колонки (row["email"]),
    # а не безымянный кортеж (row[4]). Код перестаёт ломаться при добавлении
    # колонки в середину таблицы.
    conn.row_factory = sqlite3.Row

    # WAL (Write-Ahead Logging) — режим журналирования, при котором записи
    # сначала попадают в отдельный лог-файл. Даёт две важные для нас вещи:
    #   1. Читатели не блокируют писателя. Веб-сервер принимает новый лид,
    #      пока воркер читает базу, — они не ждут друг друга. Именно это
    #      позволяет держать API и воркер разными процессами.
    #   2. При аварийном завершении процесса база восстанавливается из журнала,
    #      а не остаётся битой.
    conn.execute("PRAGMA journal_mode=WAL")

    # Если база занята другим процессом — подождать до 5 секунд, а не падать
    # сразу с "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")

    # Включает проверку внешних ключей (REFERENCES). В SQLite она по
    # историческим причинам выключена по умолчанию.
    conn.execute("PRAGMA foreign_keys=ON")


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
    """Создаёт файл базы и доводит схему до актуальной версии.

    Безопасно вызывать сколько угодно раз: уже применённые миграции
    пропускаются по номеру.
    """
    db_path = settings.db_path_absolute
    # data/ лежит в .gitignore, поэтому в свежем клоне репозитория папки нет.
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]

        for version, script in _MIGRATIONS:
            if version <= current_version:
                continue
            logger.info("Применяю миграцию #%s", version)
            # Скрипт сам открывает и закрывает транзакцию и сам выставляет
            # новый номер версии — см. ПРАВИЛО 2 выше.
            conn.executescript(script)
            current_version = version

    logger.info("База готова: %s (версия схемы %s)", db_path, current_version)


if __name__ == "__main__":
    # Ручная инициализация: py -m app.storage.database
    from app.logging_setup import setup_logging

    setup_logging()
    init_db()
