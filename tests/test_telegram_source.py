"""Диалог в Telegram: машина состояний без единого сетевого вызова.

Тестируется чистая логика: подаём словари в формате Telegram, читаем ответы
и готовый лид. Транспорт (getUpdates/sendMessage) здесь не участвует.
"""

from app.domain.lead import LeadStatus
from app.sources.telegram_source import SOURCE_NAME, TelegramDialogs
from app.storage import lead_repository, task_repository


def _message(
    text: str | None = None,
    chat_id: int = 100,
    message_id: int = 1,
    contact_phone: str | None = None,
    chat_type: str = "private",
):
    message = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": chat_id, "first_name": "Тест", "username": "test_user"},
    }
    if text is not None:
        message["text"] = text
    if contact_phone is not None:
        message["contact"] = {"phone_number": contact_phone}
    return message


def _полный_диалог(dialogs: TelegramDialogs, chat_id: int = 100, base_id: int = 10):
    dialogs.handle(_message("/start", chat_id=chat_id, message_id=base_id))
    dialogs.handle(_message("Нужен сайт для кофейни", chat_id=chat_id, message_id=base_id + 1))
    dialogs.handle(_message("Мария", chat_id=chat_id, message_id=base_id + 2))
    return dialogs.handle(
        _message("8 912 000 11 22", chat_id=chat_id, message_id=base_id + 3)
    )


def test_полный_диалог_собирает_лид():
    result = _полный_диалог(TelegramDialogs())

    lead = result.lead
    assert lead is not None
    assert lead.source == SOURCE_NAME
    assert lead.name == "Мария"
    assert lead.phone == "+79120001122", "телефон нормализован тем же правилом, что у формы"
    assert lead.message == "Нужен сайт для кофейни"
    assert lead.status is LeadStatus.NEW
    assert "принята" in result.replies[0].text


def test_диалог_работает_и_без_start():
    """Клиент сразу пишет задачу — не заставляем его знать про команды."""
    dialogs = TelegramDialogs()
    first = dialogs.handle(_message("Хочу интернет-магазин", message_id=1))

    assert first.lead is None
    assert "обращаться" in first.replies[0].text


def test_кривой_телефон_переспрашивается_и_потом_принимается():
    dialogs = TelegramDialogs()
    dialogs.handle(_message("Нужен бот", message_id=1))
    dialogs.handle(_message("Олег", message_id=2))

    отказ = dialogs.handle(_message("не скажу", message_id=3))
    assert отказ.lead is None
    assert отказ.replies[0].request_contact, "кнопка остаётся — дать шанс поделиться номером"

    успех = dialogs.handle(_message("+7 903 111-22-33", message_id=4))
    assert успех.lead is not None
    assert успех.lead.phone == "+79031112233"


def test_телефон_кнопкой_из_профиля():
    dialogs = TelegramDialogs()
    dialogs.handle(_message("Нужен лендинг", message_id=1))
    dialogs.handle(_message("Анна", message_id=2))

    result = dialogs.handle(_message(text=None, contact_phone="79striped", message_id=3))
    assert result.lead is None, "мусор в contact тоже не проходит"

    result = dialogs.handle(_message(text=None, contact_phone="+79161234567", message_id=4))
    assert result.lead is not None
    assert result.lead.phone == "+79161234567"


def test_cancel_сбрасывает_диалог():
    dialogs = TelegramDialogs()
    dialogs.handle(_message("Нужен сайт", message_id=1))
    dialogs.handle(_message("/cancel", message_id=2))

    result = dialogs.handle(_message("Совсем другая задача", message_id=3))
    assert "обращаться" in result.replies[0].text, "после отмены диалог начинается заново"


def test_чаты_не_мешают_друг_другу():
    dialogs = TelegramDialogs()
    dialogs.handle(_message("Задача первого", chat_id=1, message_id=1))
    dialogs.handle(_message("Задача второго", chat_id=2, message_id=1))
    dialogs.handle(_message("Пётр", chat_id=1, message_id=2))

    result_2 = dialogs.handle(_message("Павел", chat_id=2, message_id=2))
    assert "Павел" in result_2.replies[0].text

    lead_1 = dialogs.handle(_message("+79001110001", chat_id=1, message_id=3)).lead
    lead_2 = dialogs.handle(_message("+79001110002", chat_id=2, message_id=3)).lead
    assert lead_1.name == "Пётр" and lead_2.name == "Павел"


def test_групповые_чаты_игнорируются():
    result = TelegramDialogs().handle(_message("привет", chat_type="group"))
    assert result.replies == [] and result.lead is None


def test_передоставленный_апдейт_даёт_тот_же_ключ_дедупликации():
    """Telegram доставляет at-least-once: повтор завершающего сообщения
    должен дать тот же dedup_key, чтобы база отбросила второй лид."""
    первый = _полный_диалог(TelegramDialogs()).lead
    второй = _полный_диалог(TelegramDialogs()).lead

    assert первый.dedup_key == второй.dedup_key


def test_клавиатура_появляется_и_убирается():
    dialogs = TelegramDialogs()
    dialogs.handle(_message("Нужен сайт", message_id=1))
    просьба = dialogs.handle(_message("Иван", message_id=2))
    финал = dialogs.handle(_message("+79001112233", message_id=3))

    assert просьба.replies[0].request_contact
    assert финал.replies[0].remove_keyboard


def test_лид_из_telegram_ложится_в_ту_же_очередь(db):
    """Интеграция с ядром: источник другой, путь тот же."""
    lead = _полный_диалог(TelegramDialogs()).lead
    saved = lead_repository.save(lead, steps=["log", "qualify"])

    assert saved.is_duplicate is False
    assert lead_repository.get_by_id(saved.lead.id).source == "telegram"
    assert task_repository.get_status(saved.lead.id, "qualify") is not None

    # Передоставка того же апдейта — дубль отсекается базой, как у формы.
    повтор = lead_repository.save(_полный_диалог(TelegramDialogs()).lead, steps=["log"])
    assert повтор.is_duplicate is True
