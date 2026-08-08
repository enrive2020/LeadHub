"""Нормализация контактных данных.

Люди вводят одно и то же по-разному:

    +7 (912) 345-67-89   |   8 912 345 67 89   |   89123456789

Для человека это один номер, для строкового сравнения — три разных. Нам это
критично: на нормализованных данных строится защита от дублей (см. dedup_key
в lead.py), и без неё повторная отправка формы создаст второй лид.

Живёт в domain, а не в конкретном источнике: правило одно для всех каналов.
"""

import re

# Всё, что не цифра и не ведущий "+"
_NON_DIGITS = re.compile(r"\D")
_MULTISPACE = re.compile(r"\s+")


def collapse_spaces(value: str | None) -> str | None:
    """Убирает лишние пробелы и переносы строк по краям и внутри."""
    if value is None:
        return None
    cleaned = _MULTISPACE.sub(" ", value).strip()
    return cleaned or None


def normalize_email(value: str | None) -> str | None:
    """Приводит email к нижнему регистру. Ivan@Mail.RU и ivan@mail.ru — одно и то же."""
    if value is None:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def normalize_phone(value: str | None) -> str | None:
    """Приводит телефон к формату +7XXXXXXXXXX.

    Осознанно ориентируемся на РФ/КЗ (код 7): это целевой рынок проекта.
    Номера других стран оставляем как есть, только с ведущим "+", чтобы
    не испортить данные — лучше не тронуть, чем сломать.

    Возвращает None, если из строки не удалось достать осмысленный номер:
    пусть лучше поле будет пустым, чем содержать мусор вроде "не скажу".
    """
    if value is None:
        return None

    digits = _NON_DIGITS.sub("", value)
    if not digits:
        return None

    # 8 912 ... -> 7 912 ... (российская привычка набирать через восьмёрку)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]

    # 9123456789 -> 79123456789 (номер без кода страны)
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits

    # Слишком коротко для настоящего номера — скорее всего мусор или обрывок.
    if len(digits) < 10:
        return None

    return "+" + digits
