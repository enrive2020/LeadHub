"""Извлечение и проверка JSON из ответа модели.

Модель просили вернуть чистый JSON. Обычно она так и делает. Но «обычно» —
не то слово, на котором строят систему, через которую идут деньги клиента.

Реальные ответы, которые здесь приходится переживать:

    {"grade": "hot", ...}                     — идеальный случай
    ```json\\n{"grade": "hot", ...}\\n```      — обёрнут в markdown
    Конечно! Вот оценка:\\n{"grade": ...}      — с вежливым вступлением
    {"grade": "горячий", ...}                 — перевёл значение на русский
    {"grade": "hot", "score": "85"}           — число строкой
    {"grade": "hot"                           — обрыв по лимиту токенов

Первые три чинятся извлечением, следующие две — валидацией схемой,
последняя не чинится вовсе и должна привести к честной ошибке.
"""

import json

from pydantic import ValidationError

from app.ai.schemas import Qualification
from app.llm.errors import LLMBadOutput


def extract_json_object(raw: str) -> str:
    """Достаёт первый полный JSON-объект из произвольного текста.

    Почему не просто `json.loads(raw)`: одна строчка «Конечно! Вот ответ:»
    перед объектом — и разбор падает, хотя данные пришли правильные. Обидно
    терять готовый результат из-за вежливости модели.

    Почему не регулярное выражение: вложенные скобки регуляркой не берутся
    надёжно, а `reply_draft` вполне может содержать `{` или `}`. Поэтому
    честно считаем глубину вложенности, не забывая, что скобки внутри строк
    не считаются.
    """
    text = raw.strip()

    # Снимаем markdown-ограждение ```json ... ```
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    start = text.find("{")
    if start == -1:
        raise LLMBadOutput("В ответе модели нет ни одного объекта JSON")

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            # Экранированный символ пропускаем целиком: "\\\"" не закрывает строку.
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    # Скобки не сошлись — почти всегда это обрыв ответа по лимиту токенов.
    raise LLMBadOutput(
        "JSON в ответе модели не закрыт — вероятно, ответ обрезан по лимиту токенов"
    )


def parse_qualification(raw: str) -> Qualification:
    """Превращает сырой ответ модели в проверенный объект.

    Три ступени, и каждая может отсеять ответ:
      1. извлечь JSON из текста;
      2. разобрать его как JSON;
      3. проверить по схеме.

    Текст ошибки формулируется так, чтобы его можно было отправить обратно
    модели как претензию, — поэтому он конкретный, а не «ошибка разбора».
    """
    candidate = extract_json_object(raw)

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise LLMBadOutput(f"Невалидный JSON: {error}") from error

    if not isinstance(data, dict):
        raise LLMBadOutput("Ожидался объект JSON, а пришло значение другого типа")

    try:
        return Qualification.model_validate(data)
    except ValidationError as error:
        # Собираем компактный список претензий: "поле — что не так".
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'корень'}: {item['msg']}"
            for item in error.errors()
        )
        raise LLMBadOutput(f"Ответ не соответствует схеме — {problems}") from error
