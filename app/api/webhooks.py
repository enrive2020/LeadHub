"""Эндпоинты приёма заявок.

ЧТО ТАКОЕ ВЕБХУК. Обычно программа сама ходит за данными: "есть новые заявки?
нет? а сейчас?" — это опрос (polling), он тратит запросы впустую и всегда
опаздывает. Вебхук переворачивает схему: мы даём отправителю URL, и он САМ
стучится к нам в момент события. "Не звоните нам, мы позвоним вам".

Технически вебхук — просто HTTP-эндпоинт, который мы опубликовали наружу.
Никакой магии: сайт делает POST на наш адрес с данными формы.

ГЛАВНОЕ ПРАВИЛО ВЕБХУКА: отвечать быстро. Отправитель ждёт ответ считанные
секунды (Telegram — около 5), потом считает попытку неудачной и шлёт заявку
ещё раз. Поэтому здесь мы только сохраняем в очередь и сразу отвечаем.
Всё медленное — Google Sheets, Telegram, LLM — делает воркер отдельно.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError

from app.api.security import verify_webhook_secret
from app.logging_setup import get_logger
from app.pipeline.registry import STEP_NAMES
from app.sources.site_form import SiteFormPayload, to_lead
from app.storage import lead_repository

logger = get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["Приём заявок"])


class WebhookResponse(BaseModel):
    """Ответ отправителю."""

    ok: bool = True
    lead_id: str
    # Честно сообщаем, что заявку распознали как повтор. Отправителю это
    # позволяет не паниковать, а нам — видеть в логах реальную картину.
    duplicate: bool = False


async def parse_payload(request: Request) -> dict[str, Any]:
    """Достаёт тело запроса как словарь, независимо от формата отправителя.

    Реальность: единого формата нет. Современный фронтенд шлёт JSON, а Tilda,
    WordPress и старые лендинги — обычную HTML-форму (urlencoded). Если
    поддержать только JSON, половина клиентов не подключится.

    Такие мелочи и есть разница между "работает у меня" и "работает у клиента".
    """
    # Content-Type бывает вида "application/json; charset=utf-8" — отрезаем хвост.
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()

    if content_type == "application/json":
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Тело запроса не является корректным JSON",
            )
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ожидался JSON-объект",
            )
        return data

    if content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
        form = await request.form()
        return {key: str(value) for key, value in form.items()}

    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail=f"Неподдерживаемый Content-Type: {content_type or 'не указан'}",
    )


@router.post(
    "/site",
    response_model=WebhookResponse,
    summary="Принять заявку с формы на сайте",
    # Секрет проверяется до входа в обработчик. dependencies= используется,
    # когда результат зависимости нам не нужен — важен сам факт проверки.
    dependencies=[Depends(verify_webhook_secret)],
)
def receive_site_form(  # noqa: D401  (обработчик, а не описание)
    raw: dict[str, Any] = Depends(parse_payload),
) -> WebhookResponse:
    """Принимает заявку, сохраняет в очередь и сразу отвечает.

    ПОЧЕМУ ФУНКЦИЯ ОБЫЧНАЯ (def), А НЕ АСИНХРОННАЯ (async def).
    Внутри мы пишем в SQLite, а эта операция блокирующая — она останавливает
    поток, пока диск не ответит. В `async def` такой вызов заморозил бы весь
    событийный цикл, и сервер перестал бы принимать другие запросы на это время.
    FastAPI знает про эту ловушку: обычные `def`-обработчики он автоматически
    выполняет в отдельном потоке. Правило простое — есть блокирующий код внутри,
    пиши `def`; всё внутри асинхронное, пиши `async def`.
    """
    try:
        # Валидация по контракту источника. Всё, что не прошло, дальше не идёт.
        payload = SiteFormPayload.model_validate(raw)
    except ValidationError as error:
        # 422 = "запрос понятен, но данные не годятся". Важное отличие от 500:
        # по 4xx нормальный отправитель НЕ будет повторять запрос, и правильно —
        # повтор тех же кривых данных даст тот же результат.
        logger.info("Отклонена заявка с некорректными данными: %s", error.errors())
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {"field": ".".join(str(p) for p in err["loc"]), "error": err["msg"]}
                for err in error.errors()
            ],
        )

    # Перевод в единый формат. Дальше система не знает, что это была форма с сайта.
    lead = to_lead(payload, raw)

    # Точка, после которой лид считается принятым: он на диске вместе
    # с задачами на обработку — одной транзакцией.
    # Исключение здесь НЕ перехватываем осознанно — пусть превратится в 500,
    # и отправитель повторит запрос. Ответить 200, не сохранив лид, — худшее,
    # что можно сделать: отправитель успокоится, а заявки нет.
    #
    # Список шагов приходит из реестра пайплайна: приёмник не знает, что это
    # за шаги и что они делают. Его дело — поставить работу в очередь.
    result = lead_repository.save(lead, steps=STEP_NAMES)

    return WebhookResponse(
        lead_id=result.lead.id,
        duplicate=result.is_duplicate,
    )
