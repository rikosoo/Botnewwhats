import json
from datetime import timedelta

from sqlalchemy import select

from app.chatwoot import HUMAN_LABEL, URGENT_LABEL
from app.db.models import Consulta, LembreteEnviado, StatusConsulta
from app.tools import (
    agendar_consulta,
    alerta_urgente,
    cancelar_consulta,
    chamar_humano,
    consultar_horarios,
    execute_tool,
    remarcar_consulta,
    verificar_retorno,
)
from tests.conftest import local


async def test_horarios_respect_hours_and_min_notice(ctx):
    # Agora: seg 05/10 10:00. Antecedência 24h; terça sem atendimento; quarta 8-12 e 14-18.
    result = await consultar_horarios(ctx, "2026-10-05", "2026-10-07", "consulta")
    inicios = [h["inicio"] for h in result["horarios"]]
    assert inicios[0] == "2026-10-07T08:00"
    assert "2026-10-07T12:00" not in inicios  # almoço
    assert all(i.startswith("2026-10-07") for i in inicios)
    assert len(inicios) == 8


async def test_blocked_slot_is_never_offered(ctx, calendar):
    calendar.blocked.append((local(2026, 10, 7, 9, 30), local(2026, 10, 7, 10, 30)))
    result = await consultar_horarios(ctx, "2026-10-07", "2026-10-07", "consulta")
    inicios = [h["inicio"] for h in result["horarios"]]
    assert "2026-10-07T09:00" not in inicios
    assert "2026-10-07T10:00" not in inicios
    assert "2026-10-07T08:00" in inicios


async def test_agendar_creates_event_and_db_row(ctx, calendar):
    result = await agendar_consulta(ctx, "Maria da Silva", "2026-10-07T08:00", "consulta")
    assert result["ok"] is True
    consulta = (await ctx.session.execute(select(Consulta))).scalar_one()
    assert consulta.status == StatusConsulta.AGENDADA
    ev = calendar.events[consulta.google_event_id]
    assert ev["summary"] == "Consulta — Maria da Silva"
    assert ev["private"]["telefone"] == "+5516999990000"
    assert ev["private"]["origem"] == "bot"
    assert "Agendado via WhatsApp" in ev["description"]
    assert ctx.paciente.nome == "Maria da Silva"


async def test_agendar_revalidates_slot(ctx, calendar):
    calendar.blocked.append((local(2026, 10, 7, 8), local(2026, 10, 7, 9)))
    result = await agendar_consulta(ctx, "Maria da Silva", "2026-10-07T08:00", "consulta")
    assert "erro" in result
    assert calendar.events == {}


async def test_agendar_rejects_outside_hours(ctx):
    result = await agendar_consulta(ctx, "Maria da Silva", "2026-10-06T08:00", "consulta")  # terça
    assert "erro" in result


async def test_agendar_requires_full_name(ctx):
    result = await agendar_consulta(ctx, "Maria", "2026-10-07T08:00", "consulta")
    assert "erro" in result


async def test_retorno_only_within_45_days(ctx):
    # Sem consulta anterior: não tem direito.
    assert (await verificar_retorno(ctx))["tem_direito"] is False
    assert "erro" in await consultar_horarios(ctx, "2026-10-07", "2026-10-30", "retorno")

    # Consulta realizada há 40 dias: direito até +45 dias da consulta.
    inicio = local(2026, 8, 26, 8)
    ctx.session.add(Consulta(paciente_id=ctx.paciente.id, tipo="consulta", inicio=inicio,
                             fim=inicio + timedelta(hours=1), status=StatusConsulta.REALIZADA))
    await ctx.session.commit()
    info = await verificar_retorno(ctx)
    assert info == {"tem_direito": True, "ate": "10/10/2026"}

    result = await consultar_horarios(ctx, "2026-10-05", "2026-10-30", "retorno")
    inicios = [h["inicio"] for h in result["horarios"]]
    assert inicios and all(i <= "2026-10-10T23:59" for i in inicios)
    assert "erro" in await agendar_consulta(ctx, "Maria da Silva", "2026-10-12T08:00", "retorno")
    assert (await agendar_consulta(ctx, "Maria da Silva", "2026-10-07T08:00", "retorno"))["ok"]


async def test_retorno_denied_after_45_days(ctx):
    inicio = local(2026, 8, 1, 8)
    ctx.session.add(Consulta(paciente_id=ctx.paciente.id, tipo="consulta", inicio=inicio,
                             fim=inicio + timedelta(hours=1), status=StatusConsulta.REALIZADA))
    await ctx.session.commit()
    assert (await verificar_retorno(ctx))["tem_direito"] is False


async def test_remarcar_moves_event_and_resets_reminder(ctx, calendar):
    await agendar_consulta(ctx, "Maria da Silva", "2026-10-07T08:00", "consulta")
    consulta = (await ctx.session.execute(select(Consulta))).scalar_one()
    ctx.session.add(LembreteEnviado(consulta_id=consulta.id, tipo="d-3"))
    await ctx.session.commit()

    result = await remarcar_consulta(ctx, "2026-10-07T09:00")  # vizinho do próprio horário
    assert result["ok"] is True
    ev = calendar.events[consulta.google_event_id]
    assert ev["start"] == local(2026, 10, 7, 9)
    assert (await ctx.session.execute(select(LembreteEnviado))).scalars().all() == []


async def test_cancelar(ctx, calendar):
    await agendar_consulta(ctx, "Maria da Silva", "2026-10-07T08:00", "consulta")
    result = await cancelar_consulta(ctx)
    assert result["ok"] is True
    consulta = (await ctx.session.execute(select(Consulta))).scalar_one()
    assert consulta.status == StatusConsulta.CANCELADA
    assert calendar.events == {}


async def test_chamar_humano_handoff(ctx, chatwoot):
    await chamar_humano(ctx, "Dúvida sobre exame")
    assert chatwoot.status[42] == "open"
    assert HUMAN_LABEL in chatwoot.labels[42]
    notes = [c for cid, c, private in chatwoot.sent if private]
    assert notes and "Dúvida sobre exame" in notes[0]
    assert ctx.handed_off


async def test_alerta_urgente(ctx, chatwoot):
    ctx.settings.doctor_alert_phone = "+5516988887777"
    await alerta_urgente(ctx, "Paciente com dor intensa")
    assert {HUMAN_LABEL, URGENT_LABEL} <= chatwoot.labels[42]
    assert chatwoot.priority[42] == "urgent"
    assert chatwoot.status[42] == "open"
    assert chatwoot.templates and chatwoot.templates[0][1] == "alerta_urgente"


async def test_execute_tool_handles_bad_args(ctx):
    out = json.loads(await execute_tool(ctx, "agendar_consulta", '{"inicio": "x"}'))
    assert "erro" in out
    out = json.loads(await execute_tool(ctx, "nao_existe", "{}"))
    assert "erro" in out
