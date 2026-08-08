"""Провайдеры-заглушки: разработка и тесты без ключей и без денег.

Это не «времянка до подключения настоящей модели». Заглушки остаются в проекте
навсегда, потому что решают задачу, которую живой API не решает в принципе:

  * ПОВТОРЯЕМОСТЬ. Настоящая модель на один и тот же запрос отвечает по-разному.
    Тест, который иногда падает, хуже отсутствующего — ему перестают верить.
  * УПРАВЛЯЕМЫЕ СБОИ. Нельзя попросить Gemini «верни-ка мне обрезанный JSON,
    я проверю обработку». А проверять надо именно это: обычные ответы наш код
    переварит и без тестов.
  * СКОРОСТЬ И ЦЕНА. Тесты бегают миллисекунды и стоят ноль.

Отсюда правило: сначала система работает на заглушке, и только потом
подключается реальный провайдер.
"""

import json
import re

from app.llm.base import LLMProvider

# Слова, выдающие входящую рекламу вместо заявки.
_SPAM_MARKERS = (
    "сотрудничеств", "продвижен", "seo", "предлагаем", "рассылк",
    "инвестиц", "заработ", "крипт",
)

# Признаки готовности покупать.
_HOT_MARKERS = ("бюджет", "срочно", "сегодня", "завтра", "когда сможете", "сроки", "тз")

# Числа с намёком на деньги: "300 тысяч", "500к", "1 млн".
_MONEY = re.compile(r"\d[\d\s]*\s*(тыс|тысяч|к\b|000|млн|руб|₽)", re.IGNORECASE)


class FakeProvider(LLMProvider):
    """Правдоподобная имитация: простая эвристика вместо нейросети.

    Отвечает ровно в том формате, который мы требуем от настоящей модели,
    поэтому весь путь — промт, разбор, валидация, запись — работает по-настоящему.
    Меняется только «мозг».
    """

    name = "fake"

    @staticmethod
    def _parse_card(user: str) -> tuple[str, str]:
        """Достаёт из карточки имя и ТОЛЬКО текст клиента.

        Разбирать обязательно: если анализировать всю карточку целиком,
        длина подписей полей («Источник», «Телефон», «Email») подмешивается
        к длине сообщения, и короткая заявка выглядит развёрнутой. Я на этом
        и попался — «а сколько» получало оценку WARM вместо COLD.
        """
        name = "не указано"
        message_lines: list[str] = []
        collecting = False

        for line in user.splitlines():
            if collecting:
                message_lines.append(line)
            elif line.startswith("Имя:"):
                name = line.removeprefix("Имя:").strip()
            elif line.startswith("Сообщение клиента:"):
                collecting = True

        return name, "\n".join(message_lines).strip()

    def complete(self, *, system: str, user: str, temperature: float, max_tokens: int) -> str:
        name, message = self._parse_card(user)
        text = message.lower()

        # Имя считаем настоящим, только если в нём есть буквы: прочерки,
        # дефисы и прочие заглушки в приветствии выглядят нелепо.
        has_name = name != "не указано" and any(char.isalpha() for char in name)

        is_spam = any(marker in text for marker in _SPAM_MARKERS)
        looks_urgent = any(marker in text for marker in _HOT_MARKERS)
        has_money = bool(_MONEY.search(text))
        message_len = len(text)

        if is_spam:
            grade, score = "cold", 10
            reason = "Похоже на входящее рекламное предложение, а не на заявку клиента."
            draft = (
                "Здравствуйте! Спасибо за обращение, но предложениями о сотрудничестве "
                "мы сейчас не занимаемся."
            )
        elif has_money or (looks_urgent and message_len > 60):
            grade, score = "hot", 85
            reason = "Запрос конкретный, названы бюджет или сроки — клиент готов обсуждать работу."
            draft = (
                f"{'Здравствуйте, ' + name + '!' if has_name else 'Здравствуйте!'} "
                "Спасибо за заявку — задача понятна. Уточню пару деталей и подготовлю "
                "предложение. Когда вам удобно созвониться?"
            )
        elif message_len > 40:
            grade, score = "warm", 55
            reason = "Интерес есть, но деталей мало: нужен разговор для уточнения задачи."
            draft = (
                f"{'Здравствуйте, ' + name + '!' if has_name else 'Здравствуйте!'} "
                "Спасибо за обращение. Чтобы предложить решение, уточните, пожалуйста, "
                "задачу и желаемые сроки. Готовы созвониться в удобное вам время."
            )
        else:
            grade, score = "cold", 25
            reason = "В заявке почти нет информации — оценить перспективность невозможно."
            draft = (
                "Здравствуйте! Спасибо за обращение. Расскажите, пожалуйста, подробнее "
                "о задаче — так мы сможем предложить подходящее решение."
            )

        return json.dumps(
            {"grade": grade, "score": score, "reason": reason, "reply_draft": draft},
            ensure_ascii=False,
        )


class ScriptedProvider(LLMProvider):
    """Возвращает заранее заданные ответы по порядку — для проверки обороны.

    Именно им мы заставляем «модель» вернуть markdown-обёртку, русское значение
    grade или обрезанный JSON и смотрим, выживет ли система. На настоящем API
    такое воспроизвести невозможно.

    Когда сценарий кончается, последний ответ повторяется — так удобно
    проверять «модель упорно отвечает мусором».
    """

    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        if not responses:
            raise ValueError("Нужен хотя бы один ответ в сценарии")
        self._responses = responses
        self.calls = 0

    def complete(self, *, system: str, user: str, temperature: float, max_tokens: int) -> str:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]
