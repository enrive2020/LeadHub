"""Реестр шагов — единственное место, где перечислено, что делать с лидом.

Добавить шаг = дописать строку в ENABLED_STEPS. Ни воркер, ни приёмник,
ни база про это не узнают: воркер берёт шаг по имени из реестра, а имена
живут в таблице как обычные данные.

Порядка выполнения здесь нет намеренно. Шаги независимы и могут выполняться
в любом порядке — так падение одного не блокирует остальные. Когда в Фазе 4
появится зависимость (уведомление должно уйти уже с оценкой от LLM), мы
добавим её явно, а не будем полагаться на порядок в списке.
"""

from app.config import settings
from app.logging_setup import get_logger
from app.pipeline.base import Step
from app.pipeline.steps.log_step import LogStep
from app.pipeline.steps.qualify_step import QualifyStep
from app.pipeline.steps.sheets_step import SheetsStep
from app.pipeline.steps.telegram_step import TelegramStep

logger = get_logger(__name__)

#: Шаги, которые ставятся каждому новому лиду.
ENABLED_STEPS: list[Step] = [
    LogStep(),
    # Оценка работает всегда: на заглушке — без сети и без денег,
    # на настоящем провайдере — по ключу из .env.
    QualifyStep(),
]

# Шаг подключается только если интеграция настроена. Иначе проект остаётся
# запускаемым без Google вообще: приём и очередь работают, а человек,
# клонировавший репозиторий, видит живую систему, не заводя сервис-аккаунт.
#
# Обрати внимание, ЧЕГО здесь нет: проверки "настроен ли Google" внутри самого
# шага при каждом выполнении. Решение принимается один раз при старте, а не
# на каждом лиде.
if settings.sheets_configured:
    ENABLED_STEPS.append(SheetsStep())
else:
    logger.warning(
        "Google Sheets не настроен (нужны GOOGLE_SHEET_ID и файл ключа) — "
        "шаг записи в таблицу отключён"
    )

if settings.telegram_configured:
    ENABLED_STEPS.append(TelegramStep())
else:
    logger.warning(
        "Telegram не настроен (нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID) — "
        "шаг уведомлений отключён"
    )

#: Имена шагов — их пишем в очередь при приёме лида.
STEP_NAMES: list[str] = [step.name for step in ENABLED_STEPS]

#: Поиск шага по имени — им пользуется воркер.
STEPS_BY_NAME: dict[str, Step] = {step.name: step for step in ENABLED_STEPS}
