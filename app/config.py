"""Конфигурация приложения.

Единственное место в проекте, которое знает про переменные окружения и .env.
Весь остальной код получает готовый объект `settings` и не лезет в os.environ.

Зачем так:
  * настройки проверяются типами при старте — опечатка в .env падает сразу,
    а не через час в проде внутри случайной функции;
  * секреты не размазаны по коду;
  * подменить настройки в тестах — одна строка.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта = папка на уровень выше этого файла (app/config.py -> LeadHub/).
# Считаем его от __file__, а не от текущей рабочей директории: иначе приложение
# сломается, если запустить его из другой папки.
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Все настройки приложения в одном типизированном объекте.

    Имя поля `log_level` автоматически ищется в окружении как LOG_LEVEL.
    Приоритет источников (от высшего к низшему):
      1. переменные окружения процесса
      2. файл .env
      3. значения по умолчанию, указанные ниже
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        # Не падать, если в .env лежат переменные, которых ещё нет в этом классе
        # (там закомментированы настройки будущих фаз).
        extra="ignore",
    )

    # --- Общие ---
    app_env: Literal["dev", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Хранилище ---
    # Путь к файлу SQLite. Относительный путь считается от корня проекта.
    db_path: Path = Path("data/leadhub.db")

    # --- Веб-сервер ---
    host: str = "127.0.0.1"
    port: int = 8000

    # Общий секрет для проверки входящих вебхуков.
    # None/пусто = проверка отключена — только в dev; prod без секрета
    # не стартует (см. _prod_requires_secret ниже).
    webhook_secret: str | None = None

    # Окно дедупликации для заявок без внешнего id, в днях.
    # Внутри окна одинаковое содержимое — дубль, в следующем окне — новый лид.
    # 0 = без окна (одинаковый текст считается дублем навсегда).
    dedup_window_days: int = 1

    # --- Воркер и ретраи ---
    # Как часто воркер заглядывает в очередь, когда работы нет, сек.
    # Меньше — быстрее реакция, больше — меньше холостых запросов к базе.
    worker_poll_interval: float = 2.0
    # Сколько задач забирать за один заход.
    worker_batch_size: int = 10

    # Сколько раз пробовать шаг, прежде чем признать его безнадёжным.
    retry_max_attempts: int = 5
    # Базовая задержка перед повтором, сек. Дальше растёт вдвое: 2, 4, 8, 16...
    retry_base_delay_seconds: float = 2.0
    # Потолок задержки, сек. Без него 10-я попытка ушла бы на сутки вперёд.
    retry_max_delay_seconds: float = 300.0

    # --- Google Sheets ---
    # Файл ключа сервис-аккаунта. Относительный путь — от корня проекта.
    google_credentials_file: Path | None = None
    # ID таблицы из её адреса: docs.google.com/spreadsheets/d/<ID>/edit
    google_sheet_id: str | None = None
    # Имя листа внутри таблицы. Создастся сам, если его нет.
    google_worksheet_name: str = "Лиды"

    # --- Telegram ---
    # Токен бота от @BotFather. Это ПАРОЛЬ бота: кто им владеет, тот и бот.
    telegram_bot_token: str | None = None
    # Куда слать уведомления: id личного чата, группы или канала.
    telegram_chat_id: str | None = None

    # --- LLM ---
    # fake — заглушка без сети и без денег; openai_compatible — настоящий API.
    llm_provider: Literal["fake", "openai_compatible"] = "fake"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None

    # Низкая температура: нам нужна воспроизводимая классификация,
    # а не разнообразие формулировок. Подробнее — в app/llm/base.py.
    llm_temperature: float = 0.2
    # Потолок длины ответа. Защита и от разговорчивости, и от счёта за токены.
    #
    # ЗАПАС НУЖЕН БОЛЬШЕ, ЧЕМ КАЖЕТСЯ. Наш JSON занимает токенов 200, но у
    # моделей с рассуждением (Gemini 3.x, o-серия, Claude с thinking) в этот же
    # лимит попадают токены размышления, которых мы не видим. С лимитом 700
    # Gemini «продумывала» почти весь бюджет и обрывала JSON на середине.
    llm_max_tokens: int = 2000
    # Сколько раз просить модель исправиться, если ответ не прошёл валидацию.
    # Больше двух почти никогда не помогает — только жжёт деньги.
    llm_max_parse_attempts: int = 2

    # Сколько секунд с момента приёма заявки шаги доставки готовы ждать оценку
    # от AI, прежде чем работать без неё. Верхняя граница задержки уведомления
    # при недоступной модели — то есть обещание владельцу.
    ai_wait_seconds: float = 45.0

    # Просить провайдера гарантировать валидный JSON (response_format).
    # Снимает часть сбоев, но НЕ заменяет нашу валидацию схемой: гарантируется
    # синтаксис, а не смысл. Часть провайдеров игнорирует параметр молча.
    llm_json_mode: bool = True

    # Часовой пояс для дат, которые читает человек. Внутри система живёт
    # в UTC, а владельцу показываем его местное время.
    display_timezone: str = "Europe/Moscow"

    @property
    def telegram_configured(self) -> bool:
        """Настроены ли уведомления. Нет — шаг просто не встанет в пайплайн."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def google_credentials_path(self) -> Path | None:
        """Абсолютный путь к файлу ключа."""
        if self.google_credentials_file is None:
            return None
        if self.google_credentials_file.is_absolute():
            return self.google_credentials_file
        return BASE_DIR / self.google_credentials_file

    @property
    def sheets_configured(self) -> bool:
        """Настроена ли интеграция с Google Sheets.

        Позволяет запускать проект без Google вообще: шаг просто не встанет
        в пайплайн. Полезно и для разработки, и для того, чтобы человек,
        клонировавший репозиторий, увидел работающую систему без возни с ключами.
        """
        path = self.google_credentials_path
        return bool(self.google_sheet_id) and path is not None and path.exists()

    @model_validator(mode="after")
    def _prod_requires_secret(self) -> "Settings":
        """В prod пустой секрет вебхука — ошибка конфигурации, а не настройка.

        В dev пустой секрет удобен: локальные проверки без лишних заголовков.
        В prod та же пустота означает воронку, открытую всему интернету, —
        причём МОЛЧА: система работает, лиды идут, и дыру не видно.

        Тихое ослабление защиты — худший вид дефолта. Поэтому prod без секрета
        не запускается вовсе: упасть на старте с внятным текстом дешевле, чем
        обнаружить спам-ботов в воронке через месяц.
        """
        if self.app_env == "prod" and not (self.webhook_secret or "").strip():
            raise ValueError(
                "APP_ENV=prod требует непустой WEBHOOK_SECRET: без него приём "
                "заявок открыт любому, кто узнает адрес. Задай секрет в .env "
                "и передай его отправителям форм."
            )
        return self

    @property
    def db_path_absolute(self) -> Path:
        """Абсолютный путь к БД — им и пользуется код, работающий с файлом."""
        if self.db_path.is_absolute():
            return self.db_path
        return BASE_DIR / self.db_path

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    """Возвращает настройки, читая .env только один раз за жизнь процесса.

    @lru_cache здесь работает как кэш на функции без аргументов: первый вызов
    создаёт объект и запоминает его, все следующие отдают тот же самый.
    """
    return Settings()


# Удобный импорт: `from app.config import settings`
settings = get_settings()


if __name__ == "__main__":
    import sys

    # Windows-консоль по умолчанию не UTF-8, и кириллица превращается в кракозябры.
    sys.stdout.reconfigure(encoding="utf-8")

    # Быстрая проверка: `py -m app.config` покажет, что реально подхватилось.
    # Секреты печатаем замаскированными — привычка, которая однажды спасёт.
    print(f"BASE_DIR   = {BASE_DIR}")
    print(f"APP_ENV    = {settings.app_env}")
    print(f"LOG_LEVEL  = {settings.log_level}")
    print(f"DB_PATH    = {settings.db_path_absolute}")
    print(f"HOST:PORT  = {settings.host}:{settings.port}")
    secret = settings.webhook_secret
    print(f"WEBHOOK_SECRET = {'*' * len(secret) if secret else '(не задан)'}")
