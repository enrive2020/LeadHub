"""Источник: форма на сайте.

Единственное место в проекте, которое знает, как выглядит заявка с сайта.

Проблема реального мира: универсального формата форм не существует. Tilda шлёт
поле `Phone`, самописный лендинг — `tel`, а конструктор от местного веб-студии —
`телефон`. Заранее договориться с каждым клиентом невозможно.

Решение: принимаем распространённые варианты названий через алиасы. Вся эта
грязь заперта в одном файле и не протекает в остальную систему.
"""

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.domain.lead import Lead, build_lead

# Имя источника. Попадёт в колонку `source` и станет видно в Google Sheets.
SOURCE_NAME = "site_form"


class SiteFormPayload(BaseModel):
    """Схема входящей заявки с сайта.

    Схема здесь — это КОНТРАКТ: описание того, что мы согласны принять.
    Всё, что ему не соответствует, отсекается на входе и не попадает внутрь
    системы. Без такого контракта любой мусор из интернета доезжает до базы,
    до Google Sheets и до промта LLM.
    """

    model_config = ConfigDict(
        # Лишние поля молча игнорируем: они всё равно сохранятся целиком
        # в `raw`, а падать из-за неизвестного поля — плохая идея, форма
        # у клиента может измениться в любой момент без предупреждения.
        extra="ignore",
        # Обрезать пробелы по краям строк автоматически.
        str_strip_whitespace=True,
    )

    # AliasChoices = "поле может называться любым из этих имён".
    # Порядок важен: берётся первое найденное.
    name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("name", "Name", "fio", "имя", "Имя"),
    )
    phone: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "phone", "Phone", "tel", "telephone", "телефон", "Телефон"
        ),
    )
    email: str | None = Field(
        default=None,
        validation_alias=AliasChoices("email", "Email", "e-mail", "mail", "почта"),
    )
    message: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "message", "Message", "comment", "text", "сообщение", "Сообщение"
        ),
    )

    # Заметь: email — обычная строка, а не pydantic EmailStr с проверкой формата.
    # Это осознанно. Человек опечатался в почте, но оставил телефон — заявка
    # всё ещё ценная, и отвергать её нельзя. Строгая валидация уместна там, где
    # кривые данные ломают систему; здесь она просто теряет клиента.

    @model_validator(mode="after")
    def require_any_contact(self) -> "SiteFormPayload":
        """Хотя бы один способ связи обязателен.

        Единственное жёсткое требование. Заявка без контактов — не лид:
        владелец физически не сможет ответить. Такое лучше отклонить сразу
        и показать человеку ошибку, чем принять и потерять его молча.
        """
        if not self.phone and not self.email:
            raise ValueError("Нужен телефон или email — иначе с вами не связаться")
        return self


def to_lead(payload: SiteFormPayload, raw: dict[str, Any]) -> Lead:
    """Переводит заявку с сайта в единый формат системы.

    `raw` передаётся отдельно и намеренно: это исходные данные ДО разбора,
    со всеми полями, которых нет в нашей схеме (utm-метки, id формы, что угодно).
    Мы их не понимаем сегодня, но сохраняем — вдруг понадобятся завтра.
    """
    return build_lead(
        source=SOURCE_NAME,
        raw=raw,
        name=payload.name,
        phone=payload.phone,
        email=payload.email,
        message=payload.message,
        # external_id нет: форма на сайте не присваивает заявкам номера,
        # поэтому дедупликация пойдёт по хешу содержимого.
        external_id=None,
    )
