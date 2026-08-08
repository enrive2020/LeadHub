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
from gspread.utils import rowcol_to_a1
from requests.exceptions import RequestException

from app.ai.schemas import GRADE_LABELS, Qualification
from app.config import settings
from app.domain.lead import Lead
from app.logging_setup import get_logger
from app.pipeline.ai_wait import qualification_or_none
from app.pipeline.base import Step
from app.pipeline.errors import PermanentError, RetryableError

if TYPE_CHECKING:
    from gspread.worksheet import Worksheet

logger = get_logger(__name__)


# Заголовки таблицы. Порядок задаёт порядок колонок; менять порядок и смысл
# существующих колонок нельзя — под них уже записаны строки. ДОБАВЛЯТЬ в конец
# можно: старые строки просто останутся с пустыми хвостами.
HEADER = [
    "Дата", "ID лида", "Источник", "Имя", "Телефон", "Email", "Сообщение",
    "Оценка", "Балл", "Причина", "Черновик ответа",
]

# Колонка с ID лида (B) — по ней проверяем, не записан ли лид уже.
LEAD_ID_COLUMN = 2

# Первая AI-колонка ("Оценка"). Вычисляем из HEADER, а не пишем числом:
# при изменении набора колонок константа пересчитается сама.
AI_COLUMN_START = HEADER.index("Оценка") + 1

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
        """Приводит строку заголовков к актуальной.

        Не просто «пишет, если пусто»: набор колонок со временем растёт (в
        Фазе 4 добавились колонки AI), и у клиента уже работает лист со старым
        заголовком. Это та же задача, что миграция схемы базы, только для
        таблицы — и решать её надо так же явно.
        """
        # Лист мог быть создан узким — расширяем, иначе запись в новую колонку
        # упрётся в границы сетки.
        if worksheet.col_count < len(HEADER):
            worksheet.resize(cols=len(HEADER))

        if worksheet.row_values(1) == HEADER:
            return

        last_cell = rowcol_to_a1(1, len(HEADER))  # например "K1"
        worksheet.update(values=[HEADER], range_name=f"A1:{last_cell}")
        logger.info("Заголовки таблицы приведены к актуальным (%s колонок)", len(HEADER))

    def _worksheet_or_connect(self) -> "Worksheet":
        if self._worksheet is None:
            self._worksheet = self._connect()
        return self._worksheet

    # -- выполнение шага --------------------------------------------------

    def run(self, lead: Lead) -> None:
        # Ждём оценку до разумного предела, но не в ущерб самой записи.
        # Бросает StepDeferred, если ещё рано, — воркер вернётся позже.
        # Вызов ДО обращения к Google: незачем открывать соединение, чтобы
        # тут же отложить задачу.
        qualification = qualification_or_none(lead)

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
            known_ids = worksheet.col_values(LEAD_ID_COLUMN)
            if lead.id in known_ids:
                self._fill_ai_cells_if_blank(
                    worksheet, known_ids.index(lead.id) + 1, lead, qualification
                )
                return

            worksheet.append_row(
                self._to_row(lead, qualification),
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

    def _fill_ai_cells_if_blank(
        self,
        worksheet: "Worksheet",
        row_number: int,
        lead: Lead,
        qualification: Qualification | None,
    ) -> None:
        """Дозаписывает оценку в уже существующую строку, если её там нет.

        Сценарий: строка ушла в таблицу без оценки (модель тогда лежала),
        позже модель починили и qualify вернул задачу sheets в очередь.
        Повторный запуск попадает сюда: строка есть, но AI-ячейки пустые.

        Проверка «ячейка пуста» обязательна. Без неё повторный прогон затирал
        бы оценку, которая уже была в строке, свежепересчитанной — а владелец
        мог успеть принять решение по старой. Дозапись — да, перезапись — нет.
        """
        if qualification is None:
            logger.info("Лид %s уже есть в таблице — пропускаю", lead.id[:8])
            return

        if worksheet.cell(row_number, AI_COLUMN_START).value:
            logger.info(
                "Лид %s уже есть в таблице вместе с оценкой — пропускаю", lead.id[:8]
            )
            return

        start = rowcol_to_a1(row_number, AI_COLUMN_START)
        end = rowcol_to_a1(row_number, len(HEADER))
        worksheet.update(
            values=[self._ai_cells(qualification)],
            range_name=f"{start}:{end}",
            raw=True,
        )
        logger.info("Лид %s: оценка дописана в существующую строку", lead.id[:8])

    # -- форматирование ---------------------------------------------------

    def _to_row(self, lead: Lead, qualification: Qualification | None) -> list[str]:
        """Готовит строку таблицы.

        Внутри системы время в UTC, а владельцу показываем его местное —
        человек не должен пересчитывать часовые пояса в голове.

        Колонки AI заполняются, только если оценка есть. Пустые ячейки честнее
        прочерка или слова «нет»: сразу видно, что данных не было, а не что
        модель так решила.
        """
        local_time = lead.received_at.astimezone(self._timezone)
        row = [
            local_time.strftime("%d.%m.%Y %H:%M"),
            lead.id,
            lead.source,
            lead.name or "",
            lead.phone or "",
            lead.email or "",
            lead.message or "",
        ]

        if qualification is None:
            row += ["", "", "", ""]
        else:
            row += self._ai_cells(qualification)
        return row

    @staticmethod
    def _ai_cells(qualification: Qualification) -> list[str]:
        """AI-ячейки строки — одним списком, чтобы запись и дозапись не разъехались."""
        return [
            GRADE_LABELS[qualification.grade],
            str(qualification.score),
            qualification.reason,
            qualification.reply_draft,
        ]
