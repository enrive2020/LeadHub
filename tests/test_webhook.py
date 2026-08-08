"""Приём заявок по HTTP.

TestClient гоняет запросы через настоящее приложение, но без сети и без
поднятого сервера — быстро и без побочных эффектов.

Коды ответов проверяем не ради красоты: они управляют поведением отправителя.
Ошибиться классом — значит либо потерять лид (200 вместо 500), либо получить
вечные повторы (500 вместо 422).
"""

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app

ЗАЯВКА = {"name": "Иван Петров", "phone": "+79001112233", "message": "Нужен сайт"}


@pytest.fixture
def client(db):
    """Клиент к приложению. Зависит от db, чтобы писать во временную базу."""
    with TestClient(create_app()) as test_client:
        yield test_client


def test_заявка_принимается(client):
    response = client.post("/webhook/site", json=ЗАЯВКА)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["duplicate"] is False
    assert body["lead_id"]


def test_повтор_возвращает_тот_же_лид(client):
    """На дубль отвечаем 200, а не ошибкой: иначе отправитель будет ретраить вечно."""
    первый = client.post("/webhook/site", json=ЗАЯВКА).json()
    второй = client.post("/webhook/site", json=ЗАЯВКА).json()

    assert второй["duplicate"] is True
    assert второй["lead_id"] == первый["lead_id"]


def test_форма_с_русскими_названиями_полей(client):
    """Так шлёт Tilda: urlencoded и поля по-русски. Половина клиентов — такие."""
    response = client.post(
        "/webhook/site",
        data={"Имя": "Ольга", "Телефон": "8 912 000 11 22", "Сообщение": "Нужен бот"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_заявка_без_контактов_отклоняется(client):
    """422 — повторять бессмысленно, и отправитель это поймёт по коду."""
    response = client.post("/webhook/site", json={"name": "Аноним", "message": "перезвоните"})

    assert response.status_code == 422
    assert "телефон" in str(response.json()["detail"]).lower()


def test_битое_тело_и_чужой_тип_отклоняются(client):
    assert client.post(
        "/webhook/site", content='{"name": ', headers={"Content-Type": "application/json"}
    ).status_code == 400

    assert client.post(
        "/webhook/site", content="просто текст", headers={"Content-Type": "text/plain"}
    ).status_code == 415


def test_healthz_отвечает(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --- защита секретом -------------------------------------------------------


def test_без_секрета_проверка_выключена(client):
    """В .env секрет пуст — приём открыт, это удобно на старте."""
    assert client.post("/webhook/site", json=ЗАЯВКА).status_code == 200


def test_с_секретом_чужой_запрос_отклоняется(db, monkeypatch):
    from app.config import settings

    # Секрет строго ASCII: он едет в HTTP-заголовке, а значения заголовков
    # не-ASCII не допускают. Тест на кириллице падал именно поэтому —
    # ограничение настоящее, а не выдуманное.
    monkeypatch.setattr(settings, "webhook_secret", "s3cret-token")
    with TestClient(create_app()) as client:
        assert client.post("/webhook/site", json=ЗАЯВКА).status_code == 401
        assert client.post(
            "/webhook/site", json=ЗАЯВКА, headers={"X-Webhook-Secret": "wrong"}
        ).status_code == 401
        assert client.post(
            "/webhook/site", json=ЗАЯВКА, headers={"X-Webhook-Secret": "s3cret-token"}
        ).status_code == 200
