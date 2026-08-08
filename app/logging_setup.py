"""Настройка логирования.

Логи — это не "принты для красоты". Когда лид не доехал до CRM, единственный
способ понять почему — прочитать, что происходило. Поэтому нам важно:
  * время события (когда именно отвалилось);
  * уровень (INFO — норма, WARNING — подозрительно, ERROR — сломалось);
  * имя логгера (какой модуль это сказал).

print() ничего из этого не даёт и его нельзя выключить одной настройкой.
"""

import logging
import sys

from app.config import settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> None:
    """Настраивает корневой логгер. Вызывается один раз при старте процесса."""
    # Windows-консоль по умолчанию работает в cp866/cp1251, и русский текст
    # в логах (имена, комментарии клиентов) превращается в кракозябры.
    sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=settings.log_level,
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        stream=sys.stdout,
        # Перезаписать конфигурацию, если её уже кто-то выставил
        # (например, uvicorn при запуске веб-сервера).
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """Логгер для конкретного модуля.

    Используется как `logger = get_logger(__name__)` — тогда в логе видно,
    из какого файла пришло сообщение.
    """
    return logging.getLogger(name)
