"""Мягкое ожидание оценки: важное не должно блокироваться необязательным.

Ошибка в этой логике стоит дорого и незаметна: система будет исправно работать,
пока модель отвечает, и молча перестанет уведомлять владельца в тот день, когда
провайдер ляжет. Поэтому проверяем все ветки.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.ai.schemas import LeadGrade, Qualification
from app.config import settings
from app.pipeline.ai_wait import qualification_or_none
from app.pipeline.errors import StepDeferred
from app.storage import ai_repository, lead_repository, task_repository

ОЦЕНКА = Qualification(
    grade=LeadGrade.HOT,
    score=90,
    reason="Конкретный запрос с бюджетом.",
    reply_draft="Здравствуйте! Готовы обсудить задачу на этой неделе.",
)


def test_готовая_оценка_возвращается(db, make_lead):
    lead = lead_repository.save(make_lead(), steps=["qualify"]).lead
    ai_repository.save(lead.id, ОЦЕНКА, model="test")

    assert qualification_or_none(lead).score == 90


def test_без_шага_оценки_доставка_идёт_сразу(db, make_lead):
    """AI выключен — система обязана работать как раньше, а не ждать несуществующего."""
    lead = lead_repository.save(make_lead(), steps=["telegram"]).lead

    assert qualification_or_none(lead) is None


def test_пока_оценка_считается_шаг_откладывается(db, make_lead):
    lead = lead_repository.save(make_lead(), steps=["qualify"]).lead

    with pytest.raises(StepDeferred) as отсрочка:
        qualification_or_none(lead)

    assert отсрочка.value.delay > 0


def test_после_провала_оценки_доставка_не_ждёт(db, make_lead):
    """Как только сосед сдался — ждать нечего, доставляем немедленно.

    Именно эта ветка спасает уведомление: она срабатывает раньше таймаута.
    """
    lead = lead_repository.save(make_lead(), steps=["qualify"]).lead
    task = task_repository.fetch_ready(limit=10)[0]
    task_repository.mark_dead(task.id, lead.id, attempts=5, error="модель недоступна")

    assert qualification_or_none(lead) is None


def test_терпение_ограничено_временем(db, make_lead):
    """Страховка на случай, если сосед не умер, а завис.

    Срок считается от момента приёма заявки, поэтому подделываем именно его —
    отдельного счётчика отсрочек в системе нет и не нужно.
    """
    lead = make_lead()
    lead.received_at = datetime.now(UTC) - timedelta(
        seconds=settings.ai_wait_seconds + 1
    )
    lead_repository.save(lead, steps=["qualify"])

    assert qualification_or_none(lead) is None, "нельзя тянуть с доставкой бесконечно"
