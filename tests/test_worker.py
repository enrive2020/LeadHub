"""Воркер: как он разбирает исход шага.

Проверяем именно решения воркера, а не работу шагов: повторить, похоронить
или отложить. Ошибка в этой развилке стоит потерянного лида.
"""

import dataclasses

import pytest

from app.config import settings
from app.domain.lead import Lead
from app.pipeline import worker as worker_module
from app.pipeline.base import Step
from app.pipeline.errors import PermanentError, RetryableError, StepDeferred
from app.storage import lead_repository, task_repository
from app.storage.database import get_connection
from app.storage.task_repository import TaskStatus


def попыток(lead_id: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            "SELECT attempts FROM lead_tasks WHERE lead_id = ?", (lead_id,)
        ).fetchone()["attempts"]


class РаботающийШаг(Step):
    name = "test_step"

    def run(self, lead: Lead) -> None:
        return None


class ПадающийШаг(Step):
    name = "test_step"

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def run(self, lead: Lead) -> None:
        self.calls += 1
        raise self.error


@pytest.fixture
def подготовить(db, make_lead, monkeypatch):
    """Ставит один лид с одной задачей и подменяет реестр шагов."""

    def _подготовить(step: Step):
        # Реестр подменяем ИМЕННО в модуле воркера: он импортировал словарь
        # по значению, и правка исходного модуля до него не дойдёт.
        monkeypatch.setattr(worker_module, "STEPS_BY_NAME", {step.name: step})
        lead = lead_repository.save(make_lead(), steps=[step.name]).lead
        task = task_repository.fetch_ready(limit=10)[0]
        return lead, task

    return _подготовить


def статус(lead_id: str) -> str | None:
    return task_repository.get_status(lead_id, "test_step")


def test_успешный_шаг_помечается_выполненным(подготовить):
    lead, task = подготовить(РаботающийШаг())

    worker_module.Worker()._execute(task)

    assert статус(lead.id) == TaskStatus.DONE


def test_временный_сбой_откладывается_и_тратит_попытку(подготовить):
    lead, task = подготовить(ПадающийШаг(RetryableError("сервис лежит")))

    worker_module.Worker()._execute(task)

    assert статус(lead.id) == TaskStatus.PENDING
    assert task_repository.fetch_ready(limit=10) == [], "повтор должен быть отложен во времени"


def test_неустранимая_ошибка_хоронит_задачу_сразу(подготовить):
    """403 можно повторять до утра — доступ от этого не появится."""
    шаг = ПадающийШаг(PermanentError("нет доступа к таблице"))
    lead, task = подготовить(шаг)

    worker_module.Worker()._execute(task)

    assert статус(lead.id) == TaskStatus.DEAD
    assert шаг.calls == 1, "попытки не должны тратиться на заведомо безнадёжное"


def test_последняя_попытка_уводит_в_dead_letter(подготовить):
    lead, task = подготовить(ПадающийШаг(RetryableError("сервис лежит")))
    исчерпанная = dataclasses.replace(task, attempts=settings.retry_max_attempts - 1)

    worker_module.Worker()._execute(исчерпанная)

    assert статус(lead.id) == TaskStatus.DEAD


def test_отложенный_шаг_не_тратит_попытку(подготовить):
    """Ожидание — не неудача.

    Если бы ожидание считалось попыткой, уведомление умерло бы в dead letter
    из-за того, что задержалась необязательная AI-подсказка.
    """
    lead, task = подготовить(ПадающийШаг(StepDeferred("жду оценку AI", delay=5)))

    worker_module.Worker()._execute(task)

    assert статус(lead.id) == TaskStatus.PENDING
    assert попыток(lead.id) == 0, "отсрочка не должна расходовать право на повтор"


def test_неизвестный_шаг_откладывается_а_не_хоронится(подготовить, monkeypatch):
    """Шаг убрали из кода — задачи не должны умереть: вдруг его вернут."""
    lead, task = подготовить(РаботающийШаг())
    monkeypatch.setattr(worker_module, "STEPS_BY_NAME", {})

    worker_module.Worker()._execute(task)

    assert статус(lead.id) == TaskStatus.PENDING
