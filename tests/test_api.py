from fastapi.testclient import TestClient

from app import main
from app.config import get_settings


def _client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    monkeypatch.setenv("SCHEDULER_TOKEN", "sched")
    get_settings.cache_clear()

    class StubBot:
        async def handle_webhook(self, payload):
            return "ok"

    class StubScheduler:
        async def run_daily(self):
            return {}

    # Sem "with TestClient(...)", o lifespan não roda: usamos stubs no lugar dos clientes reais.
    main.app.state.bot, main.app.state.scheduler = StubBot(), StubScheduler()
    return TestClient(main.app)


def test_webhook_requires_token(monkeypatch):
    client = _client(monkeypatch)
    assert client.post("/webhooks/chatwoot", json={}).status_code == 401
    assert client.post("/webhooks/chatwoot?token=errado", json={}).status_code == 401
    assert client.post("/webhooks/chatwoot?token=hook", json={}).json() == {"result": "ok"}


def test_scheduler_endpoint_requires_token(monkeypatch):
    client = _client(monkeypatch)
    assert client.post("/jobs/daily").status_code == 401
    assert client.post("/jobs/daily", headers={"Authorization": "Bearer sched"}).status_code == 200
    get_settings.cache_clear()


def test_phone_masking():
    from app.security import mask_phone

    assert mask_phone("+5516999998888") == "+55*******8888"
