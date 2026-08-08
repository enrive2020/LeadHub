"""Общая подготовка тестов.

conftest.py — файл, который pytest подхватывает САМ, до всех тестов. Здесь
живут фикстуры (подготовка окружения) и, что важнее всего, изоляция.

ГЛАВНОЕ ПРАВИЛО ТЕСТОВ: они не имеют права трогать реальный мир.

Без изоляции запуск тестов писал бы строки в рабочую Google-таблицу, слал
уведомления владельцу и жёг токены модели. Такие тесты боятся запускать —
а тест, который боятся запускать, бесполезен.

Переменные окружения выставляются ДО импорта app: настройки читаются один раз
при импорте app.config, и переменные процесса приоритетнее файла .env.
"""

import os

# --- изоляция от реального мира (строго до импортов из app) ---------------
# Пустые значения гасят интеграции: шаги Sheets и Telegram не попадут
# в реестр, а LLM будет заглушкой.
os.environ["GOOGLE_SHEET_ID"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["LLM_PROVIDER"] = "fake"
os.environ["WEBHOOK_SECRET"] = ""
os.environ["APP_ENV"] = "dev"

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.domain.lead import Lead, build_lead  # noqa: E402
from app.storage.database import init_db  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Чистая база на каждый тест.

    tmp_path — встроенная фикстура pytest: своя временная папка для теста,
    которую pytest потом сам уберёт.

    Почему на КАЖДЫЙ тест, а не одна на прогон: тесты не должны зависеть от
    порядка запуска. Общая база означает, что тест начнёт падать из-за данных,
    оставленных соседом, — и искать причину будешь не там, где ошибка.

    Подмена работает потому, что путь к базе читается при каждом обращении,
    а не запоминается при старте.
    """
    monkeypatch.setattr(settings, "db_path", tmp_path / "leadhub-test.db")
    init_db()
    return tmp_path


@pytest.fixture
def make_lead():
    """Фабрика лидов для тестов.

    Возвращает функцию, а не готовый объект: тесту почти всегда нужно
    поменять одно-два поля, и фабрика избавляет от копипасты.
    """

    def _make(
        message: str = "Нужен сайт для магазина",
        name: str = "Иван Петров",
        phone: str = "+79001112233",
        email: str | None = None,
        source: str = "site_form",
    ) -> Lead:
        return build_lead(
            source=source,
            raw={"name": name, "phone": phone, "email": email, "message": message},
            name=name,
            phone=phone,
            email=email,
            message=message,
        )

    return _make
