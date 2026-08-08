"""Шаг: уведомить владельца о новой заявке в Telegram.

Почему именно Telegram, а не почта: письмо улетает в спам, читается через час
и требует SMTP-аккаунта. Telegram у владельца малого бизнеса уже открыт в
телефоне, уведомление приходит за секунду, а бот заводится за две минуты
и бесплатно.

ЛОВУШКА, О КОТОРУЮ СПОТЫКАЮТСЯ ВСЕ: бот НЕ МОЖЕТ написать человеку первым.
Пока пользователь сам не отправит боту хотя бы одно сообщение, любая попытка
писать ему заканчивается ошибкой "chat not found". Это защита Telegram от
спама, обойти её нельзя. Поэтому в инструкции по настройке первым шагом идёт
"напишите боту любое сообщение".
"""

import html
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import RequestException

from app.config import settings
from app.domain.lead import Lead
from app.logging_setup import get_logger
from app.pipeline.base import Step
from app.pipeline.errors import PermanentError, RetryableError

logger = get_logger(__name__)

API_BASE = "https://api.telegram.org"

# Сколько ждать ответа Telegram, сек.
# Таймаут ОБЯЗАТЕЛЕН. Без него requests ждёт ответа бесконечно, и одно зависшее
# соединение останавливает воркер навсегда: лиды копятся, никто не обрабатывается,
# в логах тишина. Самый неприятный вид отказа — тот, который не виден.
REQUEST_TIMEOUT = 10

# Ограничение Telegram на длину сообщения — 4096 символов. Комментарий клиента
# режем с большим запасом: экранирование может раздуть текст (один символ "<"
# превращается в четыре "&lt;"), плюс место занимают имя, контакты и подпись.
MAX_MESSAGE_TEXT = 2000

# Коды, при которых повторять бессмысленно.
#   400 — чаще всего "chat not found": боту не написали первым
#   401 — токен неверен или отозван
#   403 — пользователь заблокировал бота
#   404 — неправильный токен в URL
_PERMANENT_CODES = {400, 401, 403, 404}


class TelegramStep(Step):
    name = "telegram"

    def __init__(self) -> None:
        # Session переиспользует TCP-соединение между запросами вместо того,
        # чтобы каждый раз заново устанавливать связь и делать TLS-рукопожатие.
        # На потоке заявок это заметная экономия.
        self._session = requests.Session()
        self._timezone = ZoneInfo(settings.display_timezone)

    # -- выполнение шага --------------------------------------------------

    def run(self, lead: Lead) -> None:
        """Отправляет карточку лида.

        ЧЕСТНО ПРО ИДЕМПОТЕНТНОСТЬ. В отличие от Google Sheets, здесь нельзя
        спросить "а я это уже отправлял?" — у Telegram нет ключа идемпотентности,
        и прочитать историю чата бот не может. Значит, при падении между
        отправкой и отметкой "done" владелец получит дубль уведомления.

        Мы это осознанно принимаем. Для уведомления цена ошибок несимметрична:
        лишнее сообщение — секунда раздражения, пропущенное — потерянный клиент.
        Когда выбора нет, ошибаться надо в дешёвую сторону.
        """
        url = f"{API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": settings.telegram_chat_id,
            "text": self._render(lead),
            "parse_mode": "HTML",
            # Не разворачивать превью ссылок: карточка лида должна быть компактной.
            "link_preview_options": {"is_disabled": True},
        }

        try:
            response = self._session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except RequestException as error:
            raise RetryableError(f"Telegram недоступен: {error}") from error

        self._check_response(response)
        logger.info("Уведомление о лиде %s отправлено", lead.id[:8])

    def _check_response(self, response: requests.Response) -> None:
        """Разбирает ответ Telegram.

        Особенность этого API: успех и ошибка различаются НЕ только HTTP-кодом,
        но и полем `ok` в теле. Полагаться только на status_code — верный способ
        не заметить ошибку.
        """
        try:
            data: dict[str, Any] = response.json()
        except ValueError:
            # Не JSON — обычно это страница ошибки от прокси или балансировщика.
            raise RetryableError(
                f"Telegram вернул не-JSON (HTTP {response.status_code})"
            )

        if data.get("ok"):
            return

        code = data.get("error_code", response.status_code)
        description = data.get("description", "без описания")

        # 429 = слишком много запросов. Telegram САМ говорит, сколько ждать —
        # передаём это значение воркеру вместо своей догадки.
        if code == 429:
            retry_after = data.get("parameters", {}).get("retry_after")
            raise RetryableError(
                f"Лимит запросов Telegram: {description}",
                retry_after=retry_after,
            )

        if code in _PERMANENT_CODES:
            hint = ""
            if "chat not found" in description.lower():
                # Самая частая ошибка при настройке — подсказываем сразу,
                # чтобы человек не искал причину полчаса.
                hint = " (напишите боту любое сообщение — он не может начать диалог первым)"
            raise PermanentError(f"Telegram отказал ({code}): {description}{hint}")

        # 5xx и всё незнакомое — считаем временным.
        raise RetryableError(f"Ошибка Telegram ({code}): {description}")

    # -- форматирование ---------------------------------------------------

    def _render(self, lead: Lead) -> str:
        """Собирает карточку лида.

        БЕЗОПАСНОСТЬ: всё, что пришло от пользователя, ОБЯЗАТЕЛЬНО прогоняется
        через html.escape. Мы просим Telegram разобрать сообщение как HTML,
        поэтому клиент, написавший в форме "<b>", сломает нам разметку, и
        Telegram ответит ошибкой разбора — заявка не дойдёт.

        Это тот же класс проблем, что и SQL-инъекция: данные пользователя
        попадают туда, где их читают как код. Правило универсальное —
        экранировать на границе.
        """
        local_time = lead.received_at.astimezone(self._timezone)

        lines = ["🔔 <b>Новая заявка</b>", ""]

        if lead.name:
            lines.append(f"👤 {html.escape(lead.name)}")
        if lead.phone:
            # <code> в Telegram — тап копирует значение в буфер.
            lines.append(f"📞 <code>{html.escape(lead.phone)}</code>")
        if lead.email:
            lines.append(f"✉️ <code>{html.escape(lead.email)}</code>")

        if lead.message:
            # Обрезаем ИСХОДНЫЙ текст, а не собранное сообщение. Если резать
            # результат, легко разрубить HTML-тег пополам — Telegram не сможет
            # разобрать разметку и вернёт ошибку, то есть уведомление не дойдёт
            # именно из-за слишком длинного комментария клиента.
            message = lead.message
            if len(message) > MAX_MESSAGE_TEXT:
                message = message[:MAX_MESSAGE_TEXT] + "…"
            lines.append("")
            lines.append(f"💬 {html.escape(message)}")

        lines.append("")
        lines.append(
            f"<i>{html.escape(lead.source)} · "
            f"{local_time.strftime('%d.%m.%Y %H:%M')} · "
            f"{lead.id[:8]}</i>"
        )

        return "\n".join(lines)
