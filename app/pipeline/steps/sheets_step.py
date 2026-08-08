"""Шаг: записать лид строкой в Google Sheets.

Google Sheets — осознанно временное решение. Малому бизнесу оно нравится:
таблицу видно с телефона, можно сортировать, красить и дописывать свои колонки,
не нужно ничему учиться и ни за что платить. Когда клиент дорастёт до CRM,
рядом встанет CrmStep, а этот можно будет выключить одной строкой в реестре —
остальная система не заметит.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import gspread
from google.auth.exceptions import TransportError
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from requests.exceptions import RequestException

from app.config import settings
from app.domain.lead import Lead
from app.logging_setup import get_logger
from app.pipeline.base import Step
from app.pipeline.errors import PermanentError, RetryableError

if TYPE_CHECKING:
    from gspread.worksheet import Worksheet

logger = get_logger(__name__)


# Заголовки таблицы. Порядок задаёт порядок колонок и меняться не должен:
# существующие строки под него уже записаны.
HEADER = ["Дата", "ID лида", "Источник", "Имя", "Телефон", "Email", "Сообщение"]

# Колонка с ID лида (B) — по ней проверяем, не записан ли лид уже.
LEAD_ID_COLUMN = 2

# Коды ответа Google, при которых повтор имеет смысл.
#   429 — превышен лимит запросов (подождать и повторить — штатный сценарий)
#   5xx — сбой на стороне Google
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Коды, при которых повторять бессмысленно: проблема в настройке, а не во времени.
#   400 — некорректный запрос      401 — ключ не принят
#   403 — таблица не расшарена     404 — таблицы с таким ID нет
_PERMANENT_STATUSES = {400, 401, 403, 404}

# ВАЖНАЯ ТОНКОСТЬ. Google отдаёт 403 не только на "нет доступа", но и на
# превышение квоты. Если считать любой 403 неустранимым, то при всплеске заявок
# (или просто при пачке ретраев) лиды начнут уходить в dead letter вместо того,
# чтобы подождать полминуты и спокойно записаться.
#
# Отличить можно только по тексту причины — отдельного кода у Google нет.
# Этот список приходится держать вручную; такова цена работы с чужим API.
_RATE_LIMIT_MARKERS = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quota",
    "resource_exhausted",
)


def _status_code(error: APIError) -> int | None:
    """Достаёт HTTP-код из ошибки gspread (в разных версиях он лежит по-разному)."""
    response = getattr(error, "response", None)
    if response is not None:
        return getattr(response, "status_code", None)
    return getattr(error, "code", None)


class SheetsStep(Step):
    name = "sheets"

    def __init__(self) -> None:
        # Подключение создаётся лениво — при первом реальном обращении, а не
        # при импорте модуля. Иначе приложение не запустится вообще, если
        # Google в этот момент недоступен: сетевые походы во время импорта —
        # известный способ получить неотлаживаемый старт.
        self._worksheet: "Worksheet | None" = None
        self._timezone = ZoneInfo(settings.display_timezone)

    # -- подключение ------------------------------------------------------

    def _connect(self) -> "Worksheet":
        """Открывает лист таблицы, создавая его при необходимости."""
        credentials_path = settings.google_credentials_path
        if credentials_path is None or not credentials_path.exists():
            raise PermanentError(
                f"Файл ключа сервис-аккаунта не найден: {credentials_path}"
            )
        if not settings.google_sheet_id:
            raise PermanentError("Не задан GOOGLE_SHEET_ID")

        client = gspread.service_account(filename=str(credentials_path))

        try:
            spreadsheet = client.open_by_key(settings.google_sheet_id)
        except SpreadsheetNotFound:
            # Самая частая ошибка при подключении, поэтому подсказка развёрнутая:
            # человек почти всегда забывает именно расшарить таблицу.
            raise PermanentError(
                f"Таблица {settings.google_sheet_id} не найдена. Проверь ID и то, "
                f"что таблица расшарена на email сервис-аккаунта с правами Редактора"
            )

        try:
            worksheet = spreadsheet.worksheet(settings.google_worksheet_name)
        except WorksheetNotFound:
            logger.info("Создаю лист %r", settings.google_worksheet_name)
            worksheet = spreadsheet.add_worksheet(
                title=settings.google_worksheet_name, rows=1000, cols=len(HEADER)
            )

        self._ensure_header(worksheet)
        return worksheet

    def _ensure_header(self, worksheet: "Worksheet") -> None:
        """Дописывает строку заголовков, если лист пустой."""
        if not worksheet.row_values(1):
            worksheet.append_row(HEADER, value_input_option="RAW")
            logger.info("Записаны заголовки таблицы")

    def _worksheet_or_connect(self) -> "Worksheet":
        if self._worksheet is None:
            self._worksheet = self._connect()
        return self._worksheet

    # -- выполнение шага --------------------------------------------------

    def run(self, lead: Lead) -> None:
        try:
            worksheet = self._worksheet_or_connect()

            # ИДЕМПОТЕНТНОСТЬ НА ЧУЖОЙ СТОРОНЕ.
            # Очередь гарантирует доставку "хотя бы один раз": процесс мог
            # умереть между успешной записью в таблицу и отметкой done в базе.
            # Тогда при повторе мы бы добавили вторую такую же строку, и
            # владелец увидел бы дубль. Поэтому перед записью спрашиваем
            # у самой таблицы, нет ли там уже этого лида.
            #
            # Читаем колонку целиком — при тысячах строк это станет заметно.
            # Тогда правильный ход: хранить у себя отметку "записан в Sheets"
            # и сверяться с таблицей только при подозрении. Сейчас честнее
            # спрашивать источник правды.
            if lead.id in worksheet.col_values(LEAD_ID_COLUMN):
                logger.info("Лид %s уже есть в таблице — пропускаю", lead.id[:8])
                return

            worksheet.append_row(
                self._to_row(lead),
                # RAW, а не USER_ENTERED — иначе Sheets попытается ИСТОЛКОВАТЬ
                # значения: телефон "+79991112233" превратится в формулу и
                # покажет ошибку, а длинные числа схлопнутся в экспоненту.
                value_input_option="RAW",
            )
            logger.info("Лид %s записан в таблицу", lead.id[:8])

        except APIError as error:
            # Сбросить кэш подключения: возможно, протух токен или сменились права.
            self._worksheet = None
            self._raise_classified(error)

        except TransportError as error:
            # Не достучались до сервера авторизации Google (oauth2.googleapis.com).
            # Своя ветка нужна ради внятного сообщения: без неё в логе оказывается
            # трёхэтажная простыня про ProxyError, из которой не видно сути.
            self._worksheet = None
            raise RetryableError("Не удалось получить токен Google: сеть недоступна") from error

        except RequestException as error:
            # Сеть: таймаут, DNS, обрыв. Всегда имеет смысл повторить.
            self._worksheet = None
            raise RetryableError(f"Сеть недоступна: {error}") from error

    def _raise_classified(self, error: APIError) -> None:
        """Превращает ошибку Google в понятный воркеру класс.

        Именно здесь живёт знание "что означает ответ этого сервиса" —
        воркер такого знать не обязан и не должен.
        """
        status = _status_code(error)
        text = str(error).lower()

        # Квота маскируется под 403 — проверяем ДО общей проверки на постоянные
        # ошибки, иначе лид умрёт из-за временного лимита.
        if any(marker in text for marker in _RATE_LIMIT_MARKERS):
            raise RetryableError(f"Превышена квота Google (HTTP {status})") from error

        if status in _PERMANENT_STATUSES:
            raise PermanentError(f"Google отказал (HTTP {status}): {error}") from error

        if status in _RETRYABLE_STATUSES:
            raise RetryableError(f"Google временно недоступен (HTTP {status})") from error

        # Незнакомый код — считаем временным. Осторожная позиция: лучше зря
        # повторить, чем зря похоронить лид.
        raise RetryableError(f"Неизвестная ошибка Google (HTTP {status}): {error}") from error

    # -- форматирование ---------------------------------------------------

    def _to_row(self, lead: Lead) -> list[str]:
        """Готовит строку таблицы.

        Внутри системы время в UTC, а владельцу показываем его местное —
        человек не должен пересчитывать часовые пояса в голове.
        """
        local_time = lead.received_at.astimezone(self._timezone)
        return [
            local_time.strftime("%d.%m.%Y %H:%M"),
            lead.id,
            lead.source,
            lead.name or "",
            lead.phone or "",
            lead.email or "",
            lead.message or "",
        ]
