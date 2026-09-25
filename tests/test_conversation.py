"""Fluxos ponta a ponta com LLM mockado."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.chatwoot import HUMAN_LABEL
from app.config import Settings
from app.conversation import Bot, Incoming
from app.db.models import Consulta, LembreteEnviado, Mensagem, Paciente, StatusConsulta
from app.llm import LLMAgent
from tests.conftest import NOW, local


def _tool_call(name, args, cid="call_1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class ScriptedLLM:
    """Imita client.chat.completions.create devolvendo respostas pré-definidas."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


async def _no_sleep(_):
    return None


def _bot(settings, clinic, sessionmaker, chatwoot, calendar, responses):
    llm_client = ScriptedLLM(responses)
    bot = Bot(settings, clinic, sessionmaker, chatwoot, calendar, LLMAgent(settings, llm_client), sleep=_no_sleep)
    return bot, llm_client


def _payload(content, status="pending", labels=None, conv=42, message_type="incoming"):
    return {
        "event": "message_created",
        "message_type": message_type,
        "content": content,
        "private": False,
        "sender": {"type": "contact", "id": 5, "name": "Maria", "phone_number": "+5516999990000"},
        "conversation": {
            "id": conv, "status": status, "labels": labels or [],
            "meta": {"sender": {"id": 5, "name": "Maria", "phone_number": "+5516999990000"}},
        },
    }


async def test_price_question_formal_answer(settings, clinic, sessionmaker, chatwoot, calendar):
    bot, llm = _bot(settings, clinic, sessionmaker, chatwoot, calendar,
                    [_resp("O atendimento é exclusivamente particular, no valor de R$ 500,00.")])
    reply = await bot.process(Incoming(42, "+5516999990000", "Maria", 5, ["Quanto custa? Aceita Unimed?"]), NOW)
    assert "R$ 500,00" in reply
    system = llm.calls[0]["messages"][0]["content"]
    assert "R$ 500,00" in system and "convênio" in system
    msgs = chatwoot.public_messages(42)
    assert msgs[0] == clinic.privacidade.aviso_primeiro_contato  # primeiro contato
    assert msgs[-1] == reply


async def test_full_booking_conversation(settings, clinic, sessionmaker, chatwoot, calendar):
    bot, llm = _bot(settings, clinic, sessionmaker, chatwoot, calendar, [
        _resp(tool_calls=[_tool_call("agendar_consulta", {"nome_completo": "Maria da Silva",
                                                          "inicio": "2026-10-07T08:00", "tipo": "consulta"})]),
        _resp("Consulta agendada para quarta-feira, 07/10, às 08:00."),
    ])
    reply = await bot.process(Incoming(42, "+5516999990000", "Maria", 5, ["Pode confirmar, quarta 8h."]), NOW)
    assert "agendada" in reply
    assert len(calendar.events) == 1
    tool_msg = llm.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["ok"] is True


async def test_clinical_question_hands_off_and_bot_stops(settings, clinic, sessionmaker, chatwoot, calendar):
    bot, _ = _bot(settings, clinic, sessionmaker, chatwoot, calendar, [
        _resp(tool_calls=[_tool_call("chamar_humano", {"motivo": "Pergunta sobre creatinina"})]),
        _resp("Sua dúvida será encaminhada à Dra. Ana Paula."),
    ])
    await bot.process(Incoming(42, "+5516999990000", "Maria", 5, ["Minha creatinina deu 2,1, é grave?"]), NOW)
    assert chatwoot.status[42] == "open"
    # Próxima mensagem chega com status open + etiqueta: o bot ignora.
    result = await bot.handle_webhook(_payload("Oi?", status="open", labels=[HUMAN_LABEL]))
    assert result == "ignored:not-pending"
    result = await bot.handle_webhook(_payload("Oi?", status="pending", labels=[HUMAN_LABEL]))
    assert result == "ignored:human-label"


async def test_team_returns_conversation_to_bot(settings, clinic, sessionmaker, chatwoot, calendar):
    bot, _ = _bot(settings, clinic, sessionmaker, chatwoot, calendar, [_resp("Posso ajudar em algo mais?")])
    chatwoot.labels[42] = {HUMAN_LABEL}
    result = await bot.handle_webhook({"event": "conversation_status_changed",
                                       "id": 42, "status": "pending", "labels": [HUMAN_LABEL]})
    assert result == "returned-to-bot"
    assert HUMAN_LABEL not in chatwoot.labels[42]
    assert await bot.handle_webhook(_payload("Olá de novo")) == "queued"
    await bot.drain()
    assert chatwoot.public_messages(42)[-1] == "Posso ajudar em algo mais?"


async def test_severe_symptom_triggers_urgent_alert(settings, clinic, sessionmaker, chatwoot, calendar):
    bot, _ = _bot(settings, clinic, sessionmaker, chatwoot, calendar, [
        _resp(tool_calls=[_tool_call("alerta_urgente", {"resumo": "Falta de ar intensa"})]),
        _resp("Por favor, procure atendimento médico imediato (SAMU 192)."),
    ])
    reply = await bot.process(Incoming(42, "+5516999990000", "Maria", 5, ["Estou com muita falta de ar"]), NOW)
    assert "imediato" in reply
    assert "urgente" in chatwoot.labels[42]
    assert chatwoot.priority[42] == "urgent"


async def test_debounce_groups_messages(settings, clinic, sessionmaker, chatwoot, calendar):
    settings.debounce_seconds = 0.05
    bot, llm = _bot(settings, clinic, sessionmaker, chatwoot, calendar, [_resp("Certo!")])
    assert await bot.handle_webhook(_payload("Oi")) == "queued"
    assert await bot.handle_webhook(_payload("quero marcar consulta")) == "queued"
    await bot.drain()
    assert len(llm.calls) == 1
    assert llm.calls[0]["messages"][-1]["content"] == "Oi\nquero marcar consulta"


async def test_rate_limit(settings, clinic, sessionmaker, chatwoot, calendar):
    settings.rate_limit_per_minute = 2
    settings.debounce_seconds = 10
    bot, _ = _bot(settings, clinic, sessionmaker, chatwoot, calendar, [])
    assert await bot.handle_webhook(_payload("1")) == "queued"
    assert await bot.handle_webhook(_payload("2")) == "queued"
    assert await bot.handle_webhook(_payload("3")) == "ignored:rate-limit"
    for t in bot._timers.values():
        t.cancel()


async def test_confirm_button_updates_status_and_title(settings, clinic, sessionmaker, chatwoot, calendar):
    inicio = local(2026, 10, 8, 14)
    eid = await calendar.create_event("Consulta — Maria da Silva", "", inicio, inicio.replace(hour=15), {})
    async with sessionmaker() as s:
        p = Paciente(telefone="+5516999990000", nome="Maria da Silva")
        s.add(p)
        await s.flush()
        c = Consulta(paciente_id=p.id, tipo="consulta", inicio=inicio, fim=inicio.replace(hour=15),
                     status=StatusConsulta.AGENDADA, google_event_id=eid)
        s.add(c)
        await s.flush()
        s.add(LembreteEnviado(consulta_id=c.id, tipo="d-3"))
        await s.commit()

    bot, llm = _bot(settings, clinic, sessionmaker, chatwoot, calendar, [])
    reply = await bot.process(Incoming(42, "+5516999990000", "Maria", 5, ["Confirmar"]), NOW)
    assert "confirmada" in reply.lower()
    assert llm.calls == []
    assert calendar.events[eid]["summary"].startswith("✅")
    async with sessionmaker() as s:
        assert (await s.execute(select(Consulta))).scalar_one().status == StatusConsulta.CONFIRMADA


async def test_tool_iteration_limit_hands_off(settings, clinic, sessionmaker, chatwoot, calendar):
    loop = [_resp(tool_calls=[_tool_call("verificar_retorno", {}, f"c{i}")]) for i in range(5)]
    bot, llm = _bot(settings, clinic, sessionmaker, chatwoot, calendar, loop)
    reply = await bot.process(Incoming(42, "+5516999990000", "Maria", 5, ["?"]), NOW)
    assert len(llm.calls) == 5
    assert "equipe" in reply
    assert chatwoot.status[42] == "open"


async def test_human_messages_are_stored_as_memory(settings, clinic, sessionmaker, chatwoot, calendar):
    async with sessionmaker() as s:
        s.add(Paciente(telefone="+5516999990000"))
        await s.commit()
    bot, _ = _bot(settings, clinic, sessionmaker, chatwoot, calendar, [])
    payload = _payload("Olá, aqui é a secretária.", message_type="outgoing")
    payload["sender"] = {"type": "user", "id": 1}
    assert await bot.handle_webhook(payload) == "stored:human"
    async with sessionmaker() as s:
        assert (await s.execute(select(Mensagem))).scalar_one().papel == "humano"


@pytest.mark.parametrize("provider,url", [
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    ("groq", "https://api.groq.com/openai/v1"),
])
def test_switch_provider_only_by_env(monkeypatch, provider, url):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("LLM_MODEL", "modelo-x")
    s = Settings(_env_file=None)
    assert s.resolved_llm_base_url == url
    assert s.llm_model == "modelo-x"
