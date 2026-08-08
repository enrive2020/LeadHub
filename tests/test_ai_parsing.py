"""Разбор ответа модели — самые важные тесты в проекте.

Эти сценарии невозможно проверить на живом API: нельзя попросить Gemini
«верни-ка мне обрезанный JSON». А происходят они регулярно — на потоке
в сотню заявок такое случается еженедельно.

Именно ради этих тестов в проекте живёт ScriptedProvider.
"""

import pytest

from app.ai.parsing import extract_json_object, parse_qualification
from app.ai.schemas import LeadGrade
from app.llm.errors import LLMBadOutput

VALID = (
    '{"grade":"warm","score":60,"reason":"Интерес есть, деталей мало.",'
    '"reply_draft":"Здравствуйте! Уточните, пожалуйста, детали задачи."}'
)


# --- извлечение JSON из произвольного текста ------------------------------


def test_чистый_json_разбирается():
    assert parse_qualification(VALID).grade is LeadGrade.WARM


def test_markdown_обёртка_снимается():
    """Модель любит оборачивать ответ в ```json ... ``` — это не ошибка данных."""
    raw = "```json\n" + VALID + "\n```"
    assert parse_qualification(raw).score == 60


def test_вежливое_вступление_и_послесловие_игнорируются():
    """«Конечно! Вот оценка:» перед JSON не должно стоить нам готового ответа."""
    raw = f"Конечно! Вот результат:\n\n{VALID}\n\nНадеюсь, помог!"
    assert parse_qualification(raw).grade is LeadGrade.WARM


def test_фигурные_скобки_внутри_текста_не_ломают_разбор():
    """Ради этого случая скобки считаются вручную, а не регуляркой.

    Черновик ответа вполне может содержать { или } — регулярное выражение
    на этом споткнётся, подсчёт глубины вложенности нет.
    """
    raw = (
        '{"grade":"hot","score":80,"reason":"Конкретный запрос.",'
        '"reply_draft":"Здравствуйте! Шаблон письма выглядит так: {имя}, спасибо за заявку."}'
    )
    result = parse_qualification(raw)
    assert "{имя}" in result.reply_draft


def test_строка_со_скобкой_и_экранированием():
    """Скобка внутри строки не считается — иначе объект «закроется» раньше времени."""
    raw = '{"a": "фигурная } скобка и кавычка \\" внутри", "b": 1}'
    assert extract_json_object(raw) == raw


# --- валидация схемой ------------------------------------------------------


def test_число_строкой_приводится_автоматически():
    """score: "85" — частая мелочь, pydantic чинит её сам, повтор не нужен."""
    raw = VALID.replace('"score":60', '"score":"85"')
    assert parse_qualification(raw).score == 85


@pytest.mark.parametrize(
    "broken, ожидается_в_ошибке",
    [
        (VALID.replace('"warm"', '"горячий"'), "grade"),
        (VALID.replace('"score":60', '"score":150'), "score"),
        (VALID.replace('"score":60', '"score":-5'), "score"),
        ('{"grade":"warm","score":60}', "reply_draft"),
        (VALID.replace('"reply_draft":"Здравствуйте! Уточните, пожалуйста, детали задачи."', '"reply_draft":"ок"'), "reply_draft"),
    ],
    ids=["значение_переведено_на_русский", "балл_выше_шкалы", "балл_ниже_нуля",
         "нет_обязательного_поля", "черновик_слишком_короткий"],
)
def test_ответ_вне_контракта_отклоняется(broken, ожидается_в_ошибке):
    """Формально валидный JSON, но бесполезные данные — внутрь не попадают.

    Текст ошибки называет проблемное поле: он же уходит модели как претензия
    при повторной попытке.
    """
    with pytest.raises(LLMBadOutput) as error:
        parse_qualification(broken)
    assert ожидается_в_ошибке in str(error.value)


# --- то, что не чинится ----------------------------------------------------


def test_обрезанный_json_даёт_внятную_ошибку():
    """Обрыв по лимиту токенов должен называться своим именем, а не «ошибкой разбора»."""
    with pytest.raises(LLMBadOutput, match="не закрыт"):
        parse_qualification('{"grade":"hot","score":85,"reason":"Клиент наз')


def test_проза_вместо_json_отклоняется():
    with pytest.raises(LLMBadOutput, match="нет ни одного объекта JSON"):
        parse_qualification("Эта заявка выглядит перспективной, я бы оценил её высоко.")


def test_json_массив_вместо_объекта_отклоняется():
    with pytest.raises(LLMBadOutput):
        parse_qualification("[1, 2, 3]")
