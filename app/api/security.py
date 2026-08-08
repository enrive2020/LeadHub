"""Защита вебхука общим секретом.

Проблема: адрес вебхука — обычный URL в интернете. Кто угодно может послать
на него запрос и засорить воронку мусором или спамом. А если сверху висит LLM,
то каждая фальшивая заявка — это ещё и потраченные деньги на токены.

Простейшая рабочая защита: договориться с отправителем об общем секрете,
который он кладёт в заголовок каждого запроса. Нет секрета — нет приёма.
"""

import secrets

from fastapi import Header, HTTPException, status

from app.config import settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

SECRET_HEADER = "X-Webhook-Secret"


def verify_webhook_secret(
    # FastAPI сам достаёт заголовок из запроса: имя параметра x_webhook_secret
    # превращается в заголовок X-Webhook-Secret (подчёркивания -> дефисы).
    x_webhook_secret: str | None = Header(default=None),
) -> None:
    """Проверяет секрет. Это ЗАВИСИМОСТЬ (dependency) в терминах FastAPI.

    Внедрение зависимостей: функция объявляется отдельно и подключается к
    эндпоинту одной строкой. Проверка не размазывается по обработчикам, её
    невозможно забыть в новом источнике, и она тестируется изолированно.

    Если WEBHOOK_SECRET в .env пуст — проверка выключена. Так удобно на старте
    и в локальной разработке. В Фазе 5 сделаем так, чтобы в prod-режиме пустой
    секрет считался ошибкой конфигурации.
    """
    expected = settings.webhook_secret
    if not expected:
        return

    # compare_digest вместо обычного ==. Обычное сравнение строк прекращается
    # на первом различающемся символе, поэтому время ответа выдаёт, сколько
    # символов угадано — это атака по времени, ею реально подбирают секреты.
    # compare_digest всегда работает одинаково долго.
    if not x_webhook_secret or not secrets.compare_digest(x_webhook_secret, expected):
        logger.warning("Отклонён запрос с неверным секретом вебхука")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или отсутствующий секрет вебхука",
        )
