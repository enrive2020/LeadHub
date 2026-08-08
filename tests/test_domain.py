"""Ядро: нормализация контактов и защита от дублей."""

import pytest

from app.domain.lead import build_lead, make_dedup_key
from app.domain.normalize import collapse_spaces, normalize_email, normalize_phone


@pytest.mark.parametrize(
    "введено, ожидается",
    [
        ("+7 (912) 345-67-89", "+79123456789"),
        ("8 912 345 67 89", "+79123456789"),
        ("89123456789", "+79123456789"),
        ("912 345 67 89", "+79123456789"),
        ("+7-912-345-67-89", "+79123456789"),
        ("не скажу", None),
        ("123", None),
        ("", None),
        (None, None),
    ],
)
def test_телефон_приводится_к_единому_виду(введено, ожидается):
    """Один и тот же номер, записанный четырьмя способами, — один номер.

    Без этого дедупликация ловила бы только буквально одинаковый текст.
    """
    assert normalize_phone(введено) == ожидается


def test_email_и_пробелы():
    assert normalize_email("  Ivan@Mail.RU ") == "ivan@mail.ru"
    assert collapse_spaces("  Иван   Петров\n") == "Иван Петров"
    assert collapse_spaces("   ") is None


# --- идемпотентность -------------------------------------------------------


def test_одинаковые_заявки_дают_один_ключ():
    payload = {"name": "Иван", "phone": "+79001112233"}
    assert make_dedup_key("site_form", payload=payload) == make_dedup_key(
        "site_form", payload=payload
    )


def test_порядок_полей_не_влияет_на_ключ():
    """{"a":1,"b":2} и {"b":2,"a":1} — один и тот же словарь.

    Без сортировки ключей хеш зависел бы от порядка, в котором отправитель
    собрал JSON, и дедупликация работала бы через раз.
    """
    прямой = {"name": "Иван", "phone": "+79001112233", "utm": "vk"}
    обратный = {"utm": "vk", "phone": "+79001112233", "name": "Иван"}
    assert make_dedup_key("site_form", payload=прямой) == make_dedup_key(
        "site_form", payload=обратный
    )


def test_разные_источники_не_смешиваются():
    """Одинаковый текст из разных каналов — разные лиды, а не дубль."""
    payload = {"name": "Иван", "phone": "+79001112233"}
    assert make_dedup_key("site_form", payload=payload) != make_dedup_key(
        "telegram", payload=payload
    )


def test_внешний_id_приоритетнее_содержимого():
    """Если источник сам нумерует заявки, верим ему, а не хешу текста."""
    первый = make_dedup_key("telegram", external_id="42", payload={"text": "привет"})
    второй = make_dedup_key("telegram", external_id="42", payload={"text": "другой текст"})
    assert первый == второй


# --- сборка лида -----------------------------------------------------------


def test_build_lead_нормализует_и_сохраняет_исходник():
    raw = {"name": "  Иван   Петров ", "phone": "8 (912) 345-67-89", "utm_source": "vk"}
    lead = build_lead(
        source="site_form", raw=raw, name=raw["name"], phone=raw["phone"], message=None
    )

    assert lead.name == "Иван Петров"
    assert lead.phone == "+79123456789"
    # Поля, которых нет в нашей схеме, не выбрасываются: завтра они понадобятся.
    assert lead.raw["utm_source"] == "vk"
    assert lead.status.value == "new"
