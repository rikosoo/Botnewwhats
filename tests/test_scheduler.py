from datetime import timedelta

from sqlalchemy import select

from app.db.models import Consulta, LembreteEnviado, Paciente, StatusConsulta
from app.scheduler import Scheduler, extract_phone
from tests.conftest import local


async def _seed(sessionmaker, inicio, status=StatusConsulta.AGENDADA, conv=77, event_id="ev-x"):
    async with sessionmaker() as s:
        p = Paciente(telefone="+5516999990000", nome="Maria da Silva", chatwoot_conversation_id=conv)
        s.add(p)
        await s.flush()
        c = Consulta(paciente_id=p.id, tipo="consulta", inicio=inicio, fim=inicio + timedelta(hours=1),
                     status=status, google_event_id=event_id)
        s.add(c)
        await s.commit()
        return c.id


def _scheduler(settings, clinic, sessionmaker, chatwoot, calendar):
    return Scheduler(settings, clinic, sessionmaker, chatwoot, calendar)


async def _add_event(calendar, eid, inicio, summary="Consulta — Maria da Silva", private=None, description=""):
    calendar.events[eid] = {"summary": summary, "description": description, "start": inicio,
                            "end": inicio + timedelta(hours=1), "private": private or {}}


async def test_d_minus_3_sent_once(settings, clinic, sessionmaker, chatwoot, calendar):
    inicio = local(2026, 10, 8, 14)
    await _add_event(calendar, "ev-x", inicio)
    await _seed(sessionmaker, inicio)
    sched = _scheduler(settings, clinic, sessionmaker, chatwoot, calendar)
    now = local(2026, 10, 5, 9)

    assert (await sched.run_daily(now))["lembretes_d3"] == 1
    assert (await sched.run_daily(now))["lembretes_d3"] == 0
    assert len(chatwoot.templates) == 1
    conv, name, params = chatwoot.templates[0]
    assert (conv, name) == (77, "lembrete_consulta")
    assert params == {"1": "Maria da Silva", "2": "08/10/2026", "3": "14:00"}


async def test_failed_send_can_retry(settings, clinic, sessionmaker, chatwoot, calendar):
    inicio = local(2026, 10, 8, 14)
    await _add_event(calendar, "ev-x", inicio)
    await _seed(sessionmaker, inicio)
    sched = _scheduler(settings, clinic, sessionmaker, chatwoot, calendar)
    chatwoot.fail_templates = True
    assert (await sched.run_daily(local(2026, 10, 5, 9)))["lembretes_d3"] == 0
    chatwoot.fail_templates = False
    assert (await sched.run_daily(local(2026, 10, 5, 9, 30)))["lembretes_d3"] == 1


async def test_event_deleted_from_calendar_cancels(settings, clinic, sessionmaker, chatwoot, calendar):
    inicio = local(2026, 10, 8, 14)
    cid = await _seed(sessionmaker, inicio)  # sem evento na agenda
    sched = _scheduler(settings, clinic, sessionmaker, chatwoot, calendar)
    assert (await sched.run_daily(local(2026, 10, 5, 9)))["lembretes_d3"] == 0
    async with sessionmaker() as s:
        assert (await s.get(Consulta, cid)).status == StatusConsulta.CANCELADA


async def test_external_event_with_phone_gets_reminder(settings, clinic, sessionmaker, chatwoot, calendar):
    await _add_event(calendar, "ext1", local(2026, 10, 8, 9), summary="Consulta - João Souza",
                     description="Tel: (16) 99111-2222")
    await _add_event(calendar, "ext2", local(2026, 10, 8, 10), summary="Consulta sem telefone")
    await _add_event(calendar, "ext3", local(2026, 10, 8, 11), summary="Almoço")
    sched = _scheduler(settings, clinic, sessionmaker, chatwoot, calendar)
    assert (await sched.run_daily(local(2026, 10, 5, 9)))["lembretes_d3"] == 1
    assert chatwoot.templates[0][2]["1"] == "João Souza"
    async with sessionmaker() as s:
        p = (await s.execute(select(Paciente))).scalar_one()
        assert p.telefone == "+5516991112222"


async def test_d_plus_3_sent_once_and_not_for_cancelled(settings, clinic, sessionmaker, chatwoot, calendar):
    await _seed(sessionmaker, local(2026, 10, 2, 8))
    async with sessionmaker() as s:
        p = (await s.execute(select(Paciente))).scalar_one()
        inicio = local(2026, 10, 2, 9)
        s.add(Consulta(paciente_id=p.id, tipo="consulta", inicio=inicio, fim=inicio + timedelta(hours=1),
                       status=StatusConsulta.CANCELADA))
        await s.commit()
    sched = _scheduler(settings, clinic, sessionmaker, chatwoot, calendar)
    now = local(2026, 10, 5, 9)
    summary = await sched.run_daily(now)
    assert summary["realizadas"] == 1
    assert summary["pos_consulta_d3"] == 1
    assert (await sched.run_daily(now))["pos_consulta_d3"] == 0
    assert [t[1] for t in chatwoot.templates] == ["pos_consulta"]
    async with sessionmaker() as s:
        assert len((await s.execute(select(LembreteEnviado))).scalars().all()) == 1


def test_extract_phone():
    assert extract_phone({"description": "Telefone: +55 16 99999-8888"}) == "+5516999998888"
    assert extract_phone({"extendedProperties": {"private": {"telefone": "+5516999998888"}}}) == "+5516999998888"
    assert extract_phone({"description": "sem número"}) is None
