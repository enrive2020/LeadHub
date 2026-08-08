"""Конфигурация: prod обязан быть защищён.

Проверяем валидацию настроек напрямую, конструируя Settings руками.
`_env_file=None` отключает чтение .env — тест не должен зависеть от того,
что лежит в файле на конкретной машине.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("пустой_секрет", [None, "", "   "], ids=["none", "пустая_строка", "пробелы"])
def test_prod_без_секрета_не_запускается(пустой_секрет):
    """Забытый при деплое секрет должен уронить старт, а не молча открыть воронку."""
    with pytest.raises(ValidationError, match="WEBHOOK_SECRET"):
        Settings(app_env="prod", webhook_secret=пустой_секрет, _env_file=None)


def test_prod_с_секретом_запускается():
    settings = Settings(app_env="prod", webhook_secret="s3cret-token", _env_file=None)
    assert settings.webhook_secret == "s3cret-token"


def test_dev_без_секрета_разрешён():
    """Локальная разработка не должна требовать секрета."""
    settings = Settings(app_env="dev", webhook_secret=None, _env_file=None)
    assert settings.is_dev
