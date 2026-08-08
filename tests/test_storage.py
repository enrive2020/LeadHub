"""Хранилище: идемпотентность приёма, очередь задач, dead letter."""

from app.storage import lead_repository, task_repository
from app.storage.task_repository import TaskStatus

STEPS = ["log", "sheets"]


def test_лид_сохраняется_вместе_с_задачами(db, make_lead):
    """Лид и его задачи появляются одной транзакцией.

    Если бы задачи создавались отдельным вызовом, процесс, умерший между ними,
    оставил бы лид без работы — он лежал бы в базе, и его никто не обработал бы.
    """
    result = lead_repository.save(make_lead(), steps=STEPS)

    assert result.is_duplicate is False
    assert lead_repository.get_by_id(result.lead.id) is not None
    assert set(task_repository.stats()) == set(STEPS)


def test_повторная_отправка_не_создаёт_дубль(db, make_lead):
    """Пользователь нажал «Отправить» дважды — лид должен остаться один."""
    первый = lead_repository.save(make_lead(), steps=STEPS)
    второй = lead_repository.save(make_lead(), steps=STEPS)

    assert второй.is_duplicate is True
    assert второй.lead.id == первый.lead.id, "должен вернуться УЖЕ сохранённый лид"
    assert len(lead_repository.list_recent()) == 1


def test_задачу_можно_захватить_только_один_раз(db, make_lead):
    """Защита от гонки: два воркера не возьмут одну задачу.

    Условие `AND status = 'pending'` внутри UPDATE делает проверку и захват
    одной неделимой операцией.
    """
    lead_repository.save(make_lead(), steps=["log"])
    task = task_repository.fetch_ready(limit=10)[0]

    assert task_repository.claim(task.id) is True
    assert task_repository.claim(task.id) is False


def test_отложенная_задача_невидима_до_срока(db, make_lead):
    """Так реализован ретрай: время в будущем прячет задачу из выборки."""
    lead_repository.save(make_lead(), steps=["log"])
    task = task_repository.fetch_ready(limit=10)[0]
    task_repository.claim(task.id)
    task_repository.schedule_retry(task.id, attempts=1, delay_seconds=300, error="сбой")

    assert task_repository.fetch_ready(limit=10) == []


def test_зависшие_задачи_возвращаются_после_падения(db, make_lead):
    """Воркера убили посреди работы — задача не должна остаться «в работе» навсегда."""
    lead_repository.save(make_lead(), steps=["log"])
    task = task_repository.fetch_ready(limit=10)[0]
    task_repository.claim(task.id)  # статус processing, и тут процесс умер

    assert task_repository.reset_stale_processing() == 1
    assert len(task_repository.fetch_ready(limit=10)) == 1


def test_dead_letter_хранит_причину_и_поддаётся_возврату(db, make_lead):
    """Dead letter — это полка, а не корзина: с неё вещь можно снять обратно."""
    lead = lead_repository.save(make_lead(), steps=["log"]).lead
    task = task_repository.fetch_ready(limit=10)[0]
    task_repository.mark_dead(task.id, lead.id, attempts=5, error="сервис недоступен")

    dead = task_repository.list_dead()
    assert len(dead) == 1
    assert "сервис недоступен" in dead[0]["last_error"]
    assert lead_repository.get_by_id(lead.id).status.value == "failed"

    assert task_repository.requeue_dead("log") == 1
    вернувшаяся = task_repository.fetch_ready(limit=10)[0]
    assert вернувшаяся.attempts == 0, "после устранения причины счётчик начинается заново"


def test_статус_лида_считается_из_задач(db, make_lead):
    """Статус лида — производная величина, а не отдельная правда.

    Хранить одно и то же в двух местах — способ однажды получить расхождение.
    """
    lead = lead_repository.save(make_lead(), steps=STEPS).lead

    for task in task_repository.fetch_ready(limit=10):
        task_repository.mark_done(task.id, lead.id)

    assert lead_repository.get_by_id(lead.id).status.value == "done"


def test_выполненную_задачу_можно_вернуть_в_очередь(db, make_lead):
    """Оценка пришла после доставки — шаг должен выполниться заново и дописать её.

    Трогаются только задачи в статусе done: pending и так выполнится,
    dead ждёт осознанного requeue.
    """
    lead = lead_repository.save(make_lead(), steps=["sheets", "log"]).lead
    for task in task_repository.fetch_ready(limit=10):
        task_repository.mark_done(task.id, lead.id)

    reopened = task_repository.reopen_done(lead.id, ["sheets"])

    assert reopened == 1
    assert task_repository.get_status(lead.id, "sheets") == TaskStatus.PENDING
    assert task_repository.get_status(lead.id, "log") == TaskStatus.DONE
    assert lead_repository.get_by_id(lead.id).status.value == "processing"

    # Повторный вызов ничего не найдёт: задача уже не в done.
    assert task_repository.reopen_done(lead.id, ["sheets"]) == 0


def test_backfill_ставит_недостающие_задачи(db, make_lead):
    """Добавили шаг в работающую систему — накопленные лиды должны его получить."""
    lead_repository.save(make_lead(), steps=["log"])

    создано = task_repository.enqueue_missing(["log", "qualify"])

    assert создано == 1, "существующая задача не должна дублироваться"
    assert task_repository.get_status(
        lead_repository.list_recent()[0].id, "qualify"
    ) == TaskStatus.PENDING
