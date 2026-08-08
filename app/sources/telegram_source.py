"""Источник: диалог с клиентом в Telegram.

Единственное место в проекте, которое знает, как выглядит заявка из Telegram.
Контракт тот же, что у любого источника: на выходе — обычный `Lead`.

В отличие от формы, где все поля приходят разом, в мессенджере заявка
собирается ДИАЛОГОМ: задача -> имя -> телефон. Поэтому здесь живёт маленькая
машина состояний.

Модуль намеренно ЧИСТЫЙ: ни одного сетевого вызова. На вход — словарь
сообщения в формате Telegram, на выход — что ответить и готовый лид, когда
диалог дошёл до конца. Благодаря этому логика диалога тестируется так же
легко, как разбор ответов LLM: без сети, без токенов, за миллисекунды.
Транспорт (getUpdates/sendMessage) — в соседнем telegram_bot.py.
"""

from dataclasses import dataclass, field
from typing import Any

from app.domain.lead import Lead, build_lead
from app.domain.normalize import normalize_phone

SOURCE_NAME = "telegram"

# Сколько символов имени готовы принять: защита от вставленной простыни.
_MAX_NAME_LENGTH = 100


@dataclass
class Reply:
    """Ответ бота. Кроме текста — подсказки транспорту про клавиатуру."""

    text: str
    # Показать кнопку «Поделиться номером» (Telegram умеет отдавать телефон
    # из профиля одним нажатием — это удобнее, чем набирать руками).
    request_contact: bool = False
    # Убрать клавиатуру с кнопкой, когда она больше не нужна.
    remove_keyboard: bool = False


@dataclass
class DialogResult:
    """Итог обработки одного сообщения: что ответить и готов ли лид."""

    replies: list[Reply] = field(default_factory=list)
    lead: Lead | None = None


@dataclass
class _Dialog:
    """Состояние одного разговора. Живёт от первого сообщения до готового лида."""

    stage: str = "task"  # task -> name -> phone
    task_text: str | None = None
    name: str | None = None
    first_message_id: int | None = None


_GREETING = (
    "Здравствуйте! Я помогу оставить заявку.\n"
    "Опишите, пожалуйста, вашу задачу одним сообщением."
)


class TelegramDialogs:
    """Машина диалогов: по одному состоянию на каждый чат.

    ЧЕСТНОЕ ОГРАНИЧЕНИЕ: состояния живут в памяти процесса. Перезапуск бота
    забывает незавершённые диалоги — клиент получит просьбу начать заново.
    Это осознанный размен: диалог — эфемерное состояние интерфейса, а не
    данные заявки; готовый лид уходит в базу и переживает что угодно.
    Таблица диалогов в SQLite добавила бы миграцию и репозиторий ради
    сценария «клиент писал боту ровно в момент деплоя».
    """

    def __init__(self) -> None:
        self._dialogs: dict[int, _Dialog] = {}

    def handle(self, message: dict[str, Any]) -> DialogResult:
        """Обрабатывает одно входящее сообщение Telegram."""
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        # Работаем только с личными чатами: в группе бот-приёмник заявок
        # не имеет смысла и только шумел бы.
        if chat_id is None or chat.get("type") != "private":
            return DialogResult()

        text = (message.get("text") or "").strip()

        if text == "/cancel":
            self._dialogs.pop(chat_id, None)
            return DialogResult(
                replies=[Reply("Хорошо, отменил. Напишите, когда будете готовы.", remove_keyboard=True)]
            )

        if text == "/start":
            self._dialogs.pop(chat_id, None)
            return DialogResult(replies=[Reply(_GREETING, remove_keyboard=True)])

        dialog = self._dialogs.setdefault(chat_id, _Dialog())

        if dialog.stage == "task":
            return self._take_task(dialog, message, text)
        if dialog.stage == "name":
            return self._take_name(dialog, message, text)
        return self._take_phone(dialog, message, text, chat_id)

    # -- шаги диалога ------------------------------------------------------

    def _take_task(self, dialog: _Dialog, message: dict, text: str) -> DialogResult:
        if not text:
            # Стикер, фото, голосовое — вежливо просим текст.
            return DialogResult(replies=[Reply("Опишите, пожалуйста, задачу текстом.")])

        dialog.task_text = text
        dialog.first_message_id = message.get("message_id")
        dialog.stage = "name"
        return DialogResult(replies=[Reply("Принял. Как к вам обращаться?")])

    def _take_name(self, dialog: _Dialog, message: dict, text: str) -> DialogResult:
        if not text:
            return DialogResult(replies=[Reply("Напишите, пожалуйста, имя текстом.")])

        dialog.name = text[:_MAX_NAME_LENGTH]
        dialog.stage = "phone"
        return DialogResult(
            replies=[
                Reply(
                    f"Приятно познакомиться, {dialog.name}!\n"
                    "Оставьте, пожалуйста, номер телефона — можно кнопкой ниже.",
                    request_contact=True,
                )
            ]
        )

    def _take_phone(
        self, dialog: _Dialog, message: dict, text: str, chat_id: int
    ) -> DialogResult:
        # Телефон приходит двумя путями: набран руками или отдан кнопкой
        # «Поделиться номером» (тогда это объект contact, а не text).
        contact = message.get("contact") or {}
        raw_phone = contact.get("phone_number") or text

        # Валидация ДО принятия — той же функцией, которой ядро нормализует
        # номера. Правило одно на все источники, и именно поэтому оно живёт
        # в domain, а не в этом файле.
        if normalize_phone(raw_phone) is None:
            return DialogResult(
                replies=[
                    Reply(
                        "Не получилось распознать номер. Напишите в формате "
                        "+7 900 000-00-00 или нажмите кнопку «Поделиться номером».",
                        request_contact=True,
                    )
                ]
            )

        sender = message.get("from") or {}
        lead = build_lead(
            source=SOURCE_NAME,
            raw={
                "chat_id": chat_id,
                "username": sender.get("username"),
                "profile_name": sender.get("first_name"),
                "task": dialog.task_text,
                "name": dialog.name,
                "phone": raw_phone,
                "first_message_id": dialog.first_message_id,
                "completed_message_id": message.get("message_id"),
            },
            name=dialog.name,
            phone=raw_phone,
            message=dialog.task_text,
            # Наконец срабатывает ПЕРВАЯ стратегия дедупликации, заложенная
            # ещё в Фазе 1: источник сам гарантирует уникальность сообщения.
            # Если Telegram передоставит апдейт (их доставка — тоже
            # at-least-once), UNIQUE-индекс в базе отбросит повтор.
            external_id=f"{chat_id}:{message.get('message_id')}",
        )

        # Диалог завершён — следующее сообщение этого чата начнёт новый.
        self._dialogs.pop(chat_id, None)

        return DialogResult(
            replies=[
                Reply(
                    f"Спасибо, {lead.name}! Заявка принята.\n"
                    f"Мы свяжемся с вами по номеру {lead.phone}.",
                    remove_keyboard=True,
                )
            ],
            lead=lead,
        )
