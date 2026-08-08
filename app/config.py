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
    # None/пусто = проверка отключена (для локальной разработки).
    webhook_secret: str | None = None

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
