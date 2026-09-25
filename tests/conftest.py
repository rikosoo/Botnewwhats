from __future__ import annotations

import itertools
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings, load_clinic
from app.db.models import Base, Paciente
from app.tools import ToolContext

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Sao_Paulo")
# Segunda-feira, 05/10/2026, 10:00 em São Paulo.
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)


def local(y, m, d, h, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=TZ)


class FakeCalendar:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.blocked: list[tuple[datetime, datetime]] = []
        self._ids = itertools.count(1)

    async def busy(self, start, end):
        intervals = list(self.blocked) + [(e["start"], e["end"]) for e in self.events.values()]
        return [(s, e) for s, e in intervals if s < end and start < e]

    async def create_event(self, summary, description, start, end, private):
        eid = f"ev{next(self._ids)}"
        self.events[eid] = {"summary": summary, "description": description, "start": start, "end": end,
                            "private": private}
        return eid

    async def update_event(self, event_id, **fields):
        self.events[event_id].update({k: v for k, v in fields.items()})

    async def delete_event(self, event_id):
        self.events.pop(event_id, None)

    async def get_event(self, event_id):
        ev = self.events.get(event_id)
        if ev is None:
            raise KeyError(event_id)
        return self._as_google(event_id, ev)

    async def list_events(self, start, end):
        return [self._as_google(i, e) for i, e in self.events.items() if e["start"] < end and start < e["end"]]

    @staticmethod
    def _as_google(eid, ev):
        return {"id": eid, "summary": ev["summary"], "description": ev.get("description", ""),
                "start": {"dateTime": ev["start"].isoformat()}, "end": {"dateTime": ev["end"].isoformat()},
                "extendedProperties": {"private": ev.get("private") or {}}, "status": "confirmed"}


class FakeChatwoot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, bool]] = []
        self.templates: list[tuple[int, str, dict]] = []
        self.labels: dict[int, set[str]] = {}
        self.status: dict[int, str] = {}
        self.priority: dict[int, str] = {}
        self.fail_templates = False
        self._conv = itertools.count(900)

    async def send_message(self, conversation_id, content, private=False):
        self.sent.append((conversation_id, content, private))

    async def send_template(self, conversation_id, name, language, params, content):
        if self.fail_templates:
            raise RuntimeError("falha simulada")
        self.templates.append((conversation_id, name, params))

    async def typing(self, conversation_id, on):
        pass

    async def mark_read(self, conversation_id):
        pass

    async def set_status(self, conversation_id, status):
        self.status[conversation_id] = status

    async def set_priority(self, conversation_id, priority):
        self.priority[conversation_id] = priority

    async def get_labels(self, conversation_id):
        return sorted(self.labels.get(conversation_id, set()))

    async def add_labels(self, conversation_id, labels):
        self.labels.setdefault(conversation_id, set()).update(labels)

    async def remove_label(self, conversation_id, label):
        self.labels.get(conversation_id, set()).discard(label)

    async def get_conversation(self, conversation_id):
        return {"id": conversation_id, "status": self.status.get(conversation_id, "pending"),
                "labels": sorted(self.labels.get(conversation_id, set()))}

    async def ensure_conversation(self, phone, name=None):
        return 1, next(self._conv)

    def public_messages(self, conversation_id=None):
        return [c for cid, c, p in self.sent if not p and (conversation_id is None or cid == conversation_id)]


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite+aiosqlite://", webhook_secret="hook",
                    scheduler_token="sched", debounce_seconds=0.0, doctor_alert_phone="")


@pytest.fixture
def clinic():
    c = load_clinic(ROOT / "config" / "clinic.yaml")
    c.endereco = "Rua Exemplo, 123 — São Carlos"
    c.formas_pagamento = "PIX ou dinheiro"
    # Agenda fixa dos testes (independente do clinic.yaml de produção).
    c.atendimento.dias = {"seg": ["08:00-12:00", "14:00-18:00"], "ter": [], "qua": ["08:00-12:00", "14:00-18:00"],
                          "qui": [], "sex": ["08:00-12:00"], "sab": [], "dom": []}
    c.retorno.duracao_minutos = 60
    c.cancelamento_prazo_dias = 0
    return c


@pytest.fixture
def real_clinic():
    return load_clinic(ROOT / "config" / "clinic.yaml")


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def calendar():
    return FakeCalendar()


@pytest.fixture
def chatwoot():
    return FakeChatwoot()


@pytest_asyncio.fixture
async def ctx(sessionmaker, calendar, chatwoot, clinic, settings):
    async with sessionmaker() as session:
        paciente = Paciente(telefone="+5516999990000")
        session.add(paciente)
        await session.commit()
        yield ToolContext(session=session, paciente=paciente, conversation_id=42, chatwoot=chatwoot,
                          calendar=calendar, clinic=clinic, settings=settings, now=NOW)
