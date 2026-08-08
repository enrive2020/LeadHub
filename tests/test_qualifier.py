"""Цикл починки: что происходит, когда модель ответила не по формату.

Здесь окупается ScriptedProvider: он позволяет задать точную последовательность
ответов «модели» и проверить, что система реагирует правильно. На живом API
такое не воспроизвести — ответы недетерминированы.
"""

import pytest

from app.ai.qualifier import Qualifier
from app.ai.schemas import LeadGrade
from app.config import settings
from app.llm.errors import LLMBadOutput
from app.llm.fake import FakeProvider, ScriptedProvider

VALID = (
    '{"grade":"warm","score":60,"reason":"Интерес есть, деталей мало.",'
    '"reply_draft":"Здравствуйте! Уточните, пожалуйста, детали задачи."}'
)
BROKEN_GRADE = (
    '{"grade":"горячий","score":90,"reason":"Хорошая заявка.",'
    '"reply_draft":"Здравствуйте! Мы свяжемся с вами."}'
)


def test_годный_ответ_принимается_с_первой_попытки(make_lead):
    provider = ScriptedProvider([VALID])
    result = Qualifier(provider).qualify(make_lead())

    assert result.grade is LeadGrade.WARM
    assert provider.calls == 1, "лишний запрос к модели — это лишние деньги"


def test_модель_исправляется_после_претензии(make_lead):
    """Ключевой сценарий: первый ответ негодный, второй — верный."""
    provider = ScriptedProvider([BROKEN_GRADE, VALID])
    result = Qualifier(provider).qualify(make_lead())

    assert result.grade is LeadGrade.WARM
    assert provider.calls == 2


def test_повторный_запрос_содержит_претензию_и_исходные_данные(make_lead):
    """Проверяем СОДЕРЖАНИЕ второго запроса, а не только факт повтора.

    Слепой повтор того же запроса — ставка на случайность. Модели нужно
    показать, что именно не понравилось. И при этом нельзя потерять саму
    заявку: без данных ей не из чего собирать ответ заново.
    """
    captured: list[str] = []

    class Capturing(ScriptedProvider):
        def complete(self, *, system, user, temperature, max_tokens):
            captured.append(user)
            return super().complete(
                system=system, user=user, temperature=temperature, max_tokens=max_tokens
            )

    provider = Capturing([BROKEN_GRADE, VALID])
    Qualifier(provider).qualify(make_lead(message="Нужен магазин косметики"))

    второй = captured[1]
    assert "Нужен магазин косметики" in второй, "заявка потерялась при повторе"
    assert "grade" in второй, "модели не сказали, что именно было не так"
    assert BROKEN_GRADE[:30] in второй, "модели не показали её собственный ответ"


def test_упорный_мусор_приводит_к_честной_ошибке(make_lead):
    """Выдуманный результат хуже отсутствующего: на него будут смотреть как на настоящий."""
    provider = ScriptedProvider(["я не хочу отвечать json-ом"])

    with pytest.raises(LLMBadOutput):
        Qualifier(provider).qualify(make_lead())

    assert provider.calls == settings.llm_max_parse_attempts, (
        "число попыток должно управляться настройкой, а не быть зашито в код"
    )


def test_заглушка_выдаёт_ответ_по_контракту(make_lead):
    """Заглушка обязана быть правдоподобной, иначе тесты на ней ничего не значат."""
    result = Qualifier(FakeProvider()).qualify(
        make_lead(message="Нужен интернет-магазин, бюджет 300 тысяч, срок два месяца")
    )
    assert result.grade is LeadGrade.HOT
    assert 0 <= result.score <= 100
    assert result.reason and result.reply_draft


def test_заглушка_узнаёт_спам(make_lead):
    result = Qualifier(FakeProvider()).qualify(
        make_lead(message="Предлагаем SEO-продвижение вашего сайта, гарантия ТОП-1")
    )
    assert result.grade is LeadGrade.COLD
