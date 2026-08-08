"""Сборка FastAPI-приложения.

Здесь всё соединяется: конфигурация, логи, база, роутеры.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app import __version__
from app.api import webhooks
from app.config import BASE_DIR, settings
from app.logging_setup import get_logger, setup_logging
from app.storage import lead_repository, task_repository
from app.storage.database import init_db

logger = get_logger(__name__)

STATIC_DIR = BASE_DIR / "app" / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Код, выполняемый при старте и остановке сервера.

    Всё до `yield` — при запуске, после — при остановке. Сюда попадает то,
    что должно произойти РОВНО ОДИН РАЗ: настроить логи, создать таблицы.
    В Фазе 2 здесь же будет подниматься и корректно гаситься воркер.
    """
    setup_logging()
    init_db()
    logger.info(
        "LeadHub %s запущен на http://%s:%s (окружение: %s)",
        __version__,
        settings.host,
        settings.port,
        settings.app_env,
    )
    yield
    logger.info("LeadHub остановлен")


def create_app() -> FastAPI:
    """Фабрика приложения.

    Приложение создаётся функцией, а не как глобальная переменная на уровне
    модуля, чтобы в тестах можно было поднять отдельный экземпляр со своими
    настройками, не задевая остальные.
    """
    application = FastAPI(
        title="LeadHub",
        version=__version__,
        description=(
            "Единая точка приёма заявок. Источники приводятся к общему формату "
            "и складываются в надёжную очередь; доставка и AI-обработка идут "
            "отдельным процессом."
        ),
        lifespan=lifespan,
    )

    # CORS. Браузер запрещает странице с site.ru слать запросы на leadhub.ru,
    # если сервер явно не разрешил. Форма, которую мы отдаём сами, работала бы
    # и без этого, но реальная форма клиента живёт на его домене.
    # ВНИМАНИЕ: "*" означает "разрешено всем" — годится для разработки.
    # В Фазе 5 сузим до списка доменов клиента.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["POST", "GET"],
        allow_headers=["*"],
    )

    application.include_router(webhooks.router)

    @application.get("/healthz", tags=["Служебное"], summary="Проверка живости")
    def healthcheck() -> dict[str, object]:
        """Отвечает, жив ли сервис и что творится в очереди.

        Такую ручку опрашивает система мониторинга (или хостинг), чтобы
        перезапустить сервис, если он умер. Заодно показываем разбивку лидов
        по статусам: если там копятся failed — что-то сломано, и знать об этом
        надо раньше, чем позвонит недовольный клиент.
        """
        return {
            "status": "ok",
            "version": __version__,
            "leads": lead_repository.count_by_status(),
            "tasks": task_repository.stats(),
        }

    @application.get("/", tags=["Служебное"], include_in_schema=False)
    def demo_form() -> FileResponse:
        """Тестовая форма — имитация формы на сайте клиента."""
        return FileResponse(STATIC_DIR / "form.html")

    return application


app = create_app()
