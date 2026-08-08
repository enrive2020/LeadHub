"""Приём заявок из Telegram — третий процесс проекта.

    python -m app.sources.telegram_bot

Опрашивает Telegram методом LONG POLLING: запрос getUpdates с timeout=25
означает «держи соединение и ответь сразу, как что-то появится». Это не
наивный опрос раз в секунду — пустых ответов почти нет, задержка миллисекунды.
Вебхук здесь не подошёл бы: ему нужен публичный HTTPS-адрес, которого у
локальной машины нет.

Токен — тот же, что у уведомлений: один бот и принимает заявки от клиентов,
и шлёт карточки владельцу. Это разные чаты. Правило Telegram «только один
потребитель getUpdates на токен» не нарушено: воркер лишь отправляет.
Но ВТОРАЯ копия этого процесса даст 409 Conflict — запускать ровно одну.

Сохранение лида — прямая запись в ту же очередь, что и у вебхука. «Единая
точка входа» проекта — это модель Lead и очередь, а не HTTP-эндпоинт:
источнику незачем ходить в собственный API по кругу.
"""

import signal
import threading
from types import FrameType
from typing import Any

import requests
from requests.exceptions import RequestException

from app.config import settings
from app.logging_setup import get_logger, setup_logging
from app.pipeline.registry import STEP_NAMES
from app.sources.telegram_source import Reply, TelegramDialogs
from app.storage import lead_repository
from app.storage.database import init_db

logger = get_logger(__name__)

API_BASE = "https://api.telegram.org"

# Сколько секунд Telegram держит соединение getUpdates.
POLL_TIMEOUT = 25
# Таймаут HTTP-запроса ОБЯЗАН быть больше таймаута long polling:
# иначе мы сами оборвём соединение, которое сервер честно держит.
REQUEST_TIMEOUT = POLL_TIMEOUT + 10
# Пауза перед повтором после сетевой ошибки.
RETRY_DELAY = 3.0


class TelegramIntakeBot:
    """Цикл: получить апдейты -> прогнать через машину диалогов -> ответить."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._base_url = f"{API_BASE}/bot{settings.telegram_bot_token}"
        self._dialogs = TelegramDialogs()
        self._stop = threading.Event()
        # Смещение подтверждения: передавая offset = update_id + 1, мы говорим
        # Telegram «эти апдейты обработаны, больше не присылай».
        self._offset: int | None = None

    # -- жизненный цикл ----------------------------------------------------

    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: FrameType | None) -> None:
            if self._stop.is_set():
                raise SystemExit(1)
            logger.info("Получен сигнал %s — останавливаюсь", signum)
            self._stop.set()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def run(self) -> None:
        if not settings.telegram_bot_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN не задан — приёму заявок не с чем работать")

        # Если боту когда-то настраивали вебхук, getUpdates вернёт 409.
        # deleteWebhook идемпотентен — безопасно вызывать на каждом старте.
        self._call("deleteWebhook")

        me = self._call("getMe") or {}
        logger.info(
            "Приём заявок запущен: @%s. Напишите боту, чтобы оставить заявку",
            me.get("username", "?"),
        )

        while not self._stop.is_set():
            for update in self._get_updates():
                self._process(update)

        logger.info("Приём заявок остановлен")

    # -- работа с Telegram -------------------------------------------------

    def _get_updates(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": POLL_TIMEOUT,
            # Просим только сообщения: правки, реакции и прочее нам не нужны.
            "allowed_updates": '["message"]',
        }
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            response = self._session.get(
                f"{self._base_url}/getUpdates", params=params, timeout=REQUEST_TIMEOUT
            )
        except RequestException as error:
            logger.warning("Telegram недоступен: %s — повтор через %.0f сек", error, RETRY_DELAY)
            self._stop.wait(RETRY_DELAY)
            return []

        data = response.json() if response.content else {}
        if not data.get("ok"):
            if data.get("error_code") == 409:
                # Второй потребитель getUpdates на этот токен. Продолжать
                # бессмысленно: Telegram будет отдавать апдейты то одному,
                # то другому, и часть заявок «исчезнет».
                raise SystemExit(
                    "409 Conflict: уже запущена другая копия приёма заявок "
                    "с этим токеном. Остановите её — работать должна ровно одна."
                )
            logger.warning("Ошибка getUpdates: %s", data)
            self._stop.wait(RETRY_DELAY)
            return []

        return data.get("result", [])

    def _process(self, update: dict[str, Any]) -> None:
        # Смещение сдвигаем сразу: следующий getUpdates подтвердит этот апдейт.
        # Если процесс умрёт ДО следующего вызова, Telegram пришлёт апдейт
        # снова — то же at-least-once, что и в нашей очереди, только с их
        # стороны. Дубль готового лида отсечёт UNIQUE-индекс по external_id.
        self._offset = update["update_id"] + 1

        message = update.get("message")
        if not message:
            return

        result = self._dialogs.handle(message)

        if result.lead is not None:
            saved = lead_repository.save(result.lead, steps=STEP_NAMES)
            if saved.is_duplicate:
                logger.info("Повторный апдейт Telegram, лид уже принят: %s", saved.lead.id[:8])

        chat_id = message["chat"]["id"]
        for reply in result.replies:
            self._send_reply(chat_id, reply)

    def _send_reply(self, chat_id: int, reply: Reply) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": reply.text}

        if reply.request_contact:
            payload["reply_markup"] = {
                "keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            }
        elif reply.remove_keyboard:
            payload["reply_markup"] = {"remove_keyboard": True}

        self._call("sendMessage", payload)

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Вызов метода Bot API. Ошибки логируются, но не роняют цикл приёма.

        Ответ клиенту — вежливость, а не данные: если sendMessage не прошёл,
        терять из-за этого процесс (и чужие диалоги) нельзя.
        """
        try:
            response = self._session.post(
                f"{self._base_url}/{method}", json=payload or {}, timeout=REQUEST_TIMEOUT
            )
            data = response.json()
            if not data.get("ok"):
                logger.warning("Telegram %s отказал: %s", method, data.get("description"))
                return None
            result = data.get("result")
            return result if isinstance(result, dict) else {}
        except RequestException as error:
            logger.warning("Не удалось вызвать %s: %s", method, error)
            return None


def main() -> None:
    setup_logging()
    # Как и воркер, процесс может стартовать первым — схему готовит сам.
    init_db()

    bot = TelegramIntakeBot()
    bot.install_signal_handlers()
    bot.run()


if __name__ == "__main__":
    main()
