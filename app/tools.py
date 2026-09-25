"""Ferramentas expostas ao LLM (tool calling) e sua execução.

O telefone do paciente vem sempre do Chatwoot (ToolContext.paciente), nunca de argumentos do LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar_service import (
    CalendarBackend,
    booking_bounds,
    format_slot,
    free_slots,
    is_bookable,
    parse_day,
    parse_local,
)
from app.chatwoot import HUMAN_LABEL, URGENT_LABEL, ChatwootClient
from app.config import ClinicConfig, Settings
from app.db.models import Consulta, LembreteEnviado, Mensagem, Paciente, StatusConsulta
from app.security import mask_phone

log = logging.getLogger(__name__)

# Uma única instância do bot: um lock evita que duas conversas reservem o mesmo horário.
BOOKING_LOCK = asyncio.Lock()

MAX_SLOTS_RETURNED = 12

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "consultar_horarios",
            "description": "Lista horários livres na agenda entre duas datas. Use sempre antes de oferecer horários.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data_inicio": {"type": "string", "description": "AAAA-MM-DD"},
                    "data_fim": {"type": "string", "description": "AAAA-MM-DD"},
                    "tipo": {"type": "string", "enum": ["consulta", "retorno"]},
                },
                "required": ["data_inicio", "data_fim", "tipo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agendar_consulta",
            "description": "Agenda consulta ou retorno. Só use após confirmação explícita do paciente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome_completo": {"type": "string"},
                    "inicio": {"type": "string", "description": "AAAA-MM-DDTHH:MM (horário de Brasília)"},
                    "tipo": {"type": "string", "enum": ["consulta", "retorno"]},
                },
                "required": ["nome_completo", "inicio", "tipo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remarcar_consulta",
            "description": "Move a próxima consulta/retorno do paciente para um novo horário livre.",
            "parameters": {
                "type": "object",
                "properties": {"novo_inicio": {"type": "string", "description": "AAAA-MM-DDTHH:MM"}},
                "required": ["novo_inicio"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancelar_consulta",
            "description": "Cancela a próxima consulta/retorno do paciente.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verificar_retorno",
            "description": "Informa se o paciente tem direito a retorno e até quando.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chamar_humano",
            "description": "Transfere a conversa para a equipe (dúvida clínica, pedido de atendente, "
                           "informação que você não tem).",
            "parameters": {
                "type": "object",
                "properties": {"motivo": {"type": "string"}},
                "required": ["motivo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alerta_urgente",
            "description": "Paciente relata mal-estar intenso ou situação grave. Transfere com prioridade alta.",
            "parameters": {
                "type": "object",
                "properties": {"resumo": {"type": "string"}},
                "required": ["resumo"],
            },
        },
    },
]


@dataclass
class ToolContext:
    session: AsyncSession
    paciente: Paciente
    conversation_id: int
    chatwoot: ChatwootClient
    calendar: CalendarBackend
    clinic: ClinicConfig
    settings: Settings
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    handed_off: bool = False

    @property
    def zone(self):
        return self.settings.zone


class ToolError(Exception):
    pass


# --- consultas do paciente ------------------------------------------------

async def proxima_consulta(ctx: ToolContext) -> Consulta | None:
    stmt = (
        select(Consulta)
        .where(Consulta.paciente_id == ctx.paciente.id, Consulta.status.in_(StatusConsulta.ATIVAS),
               Consulta.inicio > ctx.now)
        .order_by(Consulta.inicio)
        .limit(1)
    )
    return (await ctx.session.execute(stmt)).scalar_one_or_none()


async def ultima_consulta_realizada(ctx: ToolContext) -> Consulta | None:
    """Última consulta (tipo consulta) já ocorrida e não cancelada."""
    stmt = (
        select(Consulta)
        .where(
            Consulta.paciente_id == ctx.paciente.id,
            Consulta.tipo == "consulta",
            Consulta.status != StatusConsulta.CANCELADA,
            Consulta.inicio <= ctx.now,
        )
        .order_by(Consulta.inicio.desc())
        .limit(1)
    )
    return (await ctx.session.execute(stmt)).scalar_one_or_none()


async def prazo_retorno(ctx: ToolContext) -> datetime | None:
    """Último instante em que um retorno pode acontecer, ou None se não houver direito."""
    ultima = await ultima_consulta_realizada(ctx)
    if ultima is None:
        return None
    last_day = ultima.inicio.astimezone(ctx.zone).date() + timedelta(days=ctx.clinic.retorno.prazo_dias)
    limite = datetime.combine(last_day, time(23, 59), tzinfo=ctx.zone)
    return limite if limite >= ctx.now else None


def _titulo(tipo: str, nome: str, confirmada: bool = False) -> str:
    prefixo = "✅ " if confirmada else ""
    return f"{prefixo}{'Retorno' if tipo == 'retorno' else 'Consulta'} — {nome}"


# --- ferramentas ----------------------------------------------------------

async def consultar_horarios(ctx: ToolContext, data_inicio: str, data_fim: str, tipo: str = "consulta") -> dict:
    tipo = "retorno" if tipo == "retorno" else "consulta"
    first, last = parse_day(data_inicio), parse_day(data_fim)
    earliest, latest = booking_bounds(ctx.clinic, ctx.now)
    not_after = None
    if tipo == "retorno":
        not_after = await prazo_retorno(ctx)
        if not_after is None:
            return {"erro": "Paciente não tem direito a retorno (sem consulta nos últimos "
                            f"{ctx.clinic.retorno.prazo_dias} dias). Ofereça uma consulta."}
        latest = min(latest, not_after)
    first = max(first, earliest.astimezone(ctx.zone).date())
    last = min(last, latest.astimezone(ctx.zone).date())
    if last < first:
        return {"horarios": [], "observacao": "Nenhuma data disponível nesse intervalo."}

    range_start = datetime.combine(first, time.min, tzinfo=ctx.zone)
    range_end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=ctx.zone)
    busy = await ctx.calendar.busy(range_start, range_end)
    slots = free_slots(ctx.clinic, tipo, first, last, busy, ctx.now, ctx.zone, not_after)
    return {
        "tipo": tipo,
        "horarios": [
            {"inicio": s[0].astimezone(ctx.zone).strftime("%Y-%m-%dT%H:%M"), "descricao": format_slot(s[0], ctx.zone)}
            for s in slots[:MAX_SLOTS_RETURNED]
        ],
        "total_encontrado": len(slots),
    }


async def _slot_livre(ctx: ToolContext, tipo: str, inicio: datetime, ignorar_evento: Consulta | None = None,
                      not_after: datetime | None = None) -> bool:
    fim = inicio + timedelta(minutes=ctx.clinic.duracao(tipo))
    busy = await ctx.calendar.busy(inicio - timedelta(minutes=1), fim + timedelta(minutes=1))
    if ignorar_evento is not None:
        busy = [b for b in busy if not (b[0] == ignorar_evento.inicio and b[1] == ignorar_evento.fim)]
    return is_bookable(ctx.clinic, tipo, inicio, busy, ctx.now, ctx.zone, not_after)


async def agendar_consulta(ctx: ToolContext, nome_completo: str, inicio: str, tipo: str = "consulta") -> dict:
    tipo = "retorno" if tipo == "retorno" else "consulta"
    nome = " ".join(nome_completo.split())
    if len(nome.split()) < 2:
        return {"erro": "Peça o nome completo do paciente."}
    start = parse_local(inicio, ctx.zone)

    existente = await proxima_consulta(ctx)
    if existente is not None:
        return {"erro": "Paciente já possui agendamento em "
                        f"{format_slot(existente.inicio, ctx.zone)} ({existente.tipo}). "
                        "Pergunte se deseja remarcar."}
    not_after = None
    if tipo == "retorno":
        not_after = await prazo_retorno(ctx)
        if not_after is None:
            return {"erro": "Paciente não tem direito a retorno. Ofereça uma consulta."}

    async with BOOKING_LOCK:
        if not await _slot_livre(ctx, tipo, start, not_after=not_after):
            return {"erro": "Esse horário não está mais disponível. Consulte os horários novamente."}
        end = start + timedelta(minutes=ctx.clinic.duracao(tipo))
        ctx.paciente.nome = nome
        consulta = Consulta(paciente_id=ctx.paciente.id, tipo=tipo, inicio=start, fim=end,
                            status=StatusConsulta.AGENDADA, origem="bot")
        ctx.session.add(consulta)
        await ctx.session.flush()
        event_id = await ctx.calendar.create_event(
            summary=_titulo(tipo, nome),
            description=f"Telefone: {ctx.paciente.telefone}\nAgendado via WhatsApp",
            start=start,
            end=end,
            private={"telefone": ctx.paciente.telefone, "tipo": tipo,
                     "consulta_id": str(consulta.id), "origem": "bot"},
        )
        consulta.google_event_id = event_id
        await ctx.session.commit()
    log.info("Agendado %s consulta_id=%s paciente=%s", tipo, consulta.id, mask_phone(ctx.paciente.telefone))
    return {"ok": True, "tipo": tipo, "quando": format_slot(start, ctx.zone), "nome": nome,
            "endereco": ctx.clinic.endereco}


async def remarcar_consulta(ctx: ToolContext, novo_inicio: str) -> dict:
    consulta = await proxima_consulta(ctx)
    if consulta is None:
        return {"erro": "Não encontrei agendamento futuro para este paciente."}
    start = parse_local(novo_inicio, ctx.zone)
    not_after = await prazo_retorno(ctx) if consulta.tipo == "retorno" else None
    async with BOOKING_LOCK:
        if not await _slot_livre(ctx, consulta.tipo, start, ignorar_evento=consulta, not_after=not_after):
            return {"erro": "Esse horário não está disponível. Consulte os horários novamente."}
        end = start + timedelta(minutes=ctx.clinic.duracao(consulta.tipo))
        if consulta.google_event_id:
            await ctx.calendar.update_event(
                consulta.google_event_id, start=start, end=end,
                summary=_titulo(consulta.tipo, ctx.paciente.nome or ""),
            )
        antigo = consulta.inicio
        consulta.inicio, consulta.fim, consulta.status = start, end, StatusConsulta.AGENDADA
        # O lembrete D-3 deve ser enviado de novo para a nova data.
        await ctx.session.execute(
            delete(LembreteEnviado).where(LembreteEnviado.consulta_id == consulta.id, LembreteEnviado.tipo == "d-3")
        )
        await ctx.session.commit()
    return {"ok": True, "de": format_slot(antigo, ctx.zone), "para": format_slot(start, ctx.zone)}


async def cancelar_consulta(ctx: ToolContext) -> dict:
    consulta = await proxima_consulta(ctx)
    if consulta is None:
        return {"erro": "Não encontrei agendamento futuro para este paciente."}
    if consulta.google_event_id:
        await ctx.calendar.delete_event(consulta.google_event_id)
    consulta.status = StatusConsulta.CANCELADA
    await ctx.session.commit()
    return {"ok": True, "cancelada": format_slot(consulta.inicio, ctx.zone), "tipo": consulta.tipo}


async def verificar_retorno(ctx: ToolContext) -> dict:
    limite = await prazo_retorno(ctx)
    if limite is None:
        return {"tem_direito": False,
                "motivo": f"Nenhuma consulta realizada nos últimos {ctx.clinic.retorno.prazo_dias} dias."}
    return {"tem_direito": True, "ate": limite.astimezone(ctx.zone).strftime("%d/%m/%Y")}


async def _resumo_conversa(ctx: ToolContext, n: int = 8) -> str:
    stmt = (
        select(Mensagem).where(Mensagem.paciente_id == ctx.paciente.id)
        .order_by(Mensagem.criado_em.desc(), Mensagem.id.desc()).limit(n)
    )
    msgs = list(reversed((await ctx.session.execute(stmt)).scalars().all()))
    return "\n".join(f"[{m.papel}] {m.conteudo[:300]}" for m in msgs)


async def chamar_humano(ctx: ToolContext, motivo: str, urgente: bool = False) -> dict:
    titulo = "🚨 ALERTA URGENTE" if urgente else "🙋 Transferido para atendimento humano"
    nota = (
        f"{titulo}\nMotivo: {motivo}\n"
        f"Paciente: {ctx.paciente.nome or 'não informado'}\n\n"
        f"Últimas mensagens:\n{await _resumo_conversa(ctx)}\n\n"
        "Para devolver ao bot: mude o status da conversa para 'Pendente'."
    )
    cid = ctx.conversation_id
    await ctx.chatwoot.add_labels(cid, [HUMAN_LABEL] + ([URGENT_LABEL] if urgente else []))
    if urgente:
        await ctx.chatwoot.set_priority(cid, "urgent")
    await ctx.chatwoot.send_message(cid, nota, private=True)
    await ctx.chatwoot.set_status(cid, "open")
    ctx.handed_off = True
    log.info("Handoff conversa=%s urgente=%s", cid, urgente)
    return {"ok": True, "instrucao": "Informe ao paciente, com cordialidade, que a equipe dará continuidade."}


async def alerta_urgente(ctx: ToolContext, resumo: str) -> dict:
    result = await chamar_humano(ctx, resumo, urgente=True)
    phone = ctx.settings.doctor_alert_phone
    if phone:
        try:
            _, conv = await ctx.chatwoot.ensure_conversation(phone, ctx.clinic.medica)
            await ctx.chatwoot.add_labels(conv, [HUMAN_LABEL])  # o bot nunca responde a esta conversa
            nome = ctx.paciente.nome or "paciente"
            await ctx.chatwoot.send_template(
                conv, ctx.clinic.templates.alerta_urgente, ctx.clinic.templates.idioma,
                {"1": nome, "2": ctx.paciente.telefone},
                f"Alerta urgente: {nome} ({ctx.paciente.telefone}) relatou situação grave. Veja o Chatwoot.",
            )
        except Exception:  # o alerta ao celular é opcional; o handoff já foi feito
            log.exception("Falha ao enviar alerta para o celular da médica")
    return result


TOOL_FUNCTIONS = {
    "consultar_horarios": consultar_horarios,
    "agendar_consulta": agendar_consulta,
    "remarcar_consulta": remarcar_consulta,
    "cancelar_consulta": cancelar_consulta,
    "verificar_retorno": verificar_retorno,
    "chamar_humano": chamar_humano,
    "alerta_urgente": alerta_urgente,
}


async def _reset(ctx: ToolContext) -> None:
    await ctx.session.rollback()
    await ctx.session.refresh(ctx.paciente)


async def execute_tool(ctx: ToolContext, name: str, raw_args: str | None) -> str:
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return json.dumps({"erro": f"Ferramenta desconhecida: {name}"})
    try:
        args = json.loads(raw_args or "{}") or {}
        if name == "chamar_humano":
            args = {"motivo": args.get("motivo", "")}
        result = await func(ctx, **args)
    except (TypeError, ValueError) as exc:
        await _reset(ctx)
        result = {"erro": f"Parâmetros inválidos: {exc}"}
    except Exception:
        await _reset(ctx)
        log.exception("Erro executando ferramenta %s", name)
        result = {"erro": "Falha técnica ao executar a ação. Use chamar_humano."}
    return json.dumps(result, ensure_ascii=False)
