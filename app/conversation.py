"""Orquestra o atendimento: filtra webhooks do Chatwoot, agrupa mensagens (debounce),
chama o LLM e responde de forma humanizada."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.calendar_service import CalendarBackend, format_slot
from app.chatwoot import HUMAN_LABEL, ChatwootClient
from app.config import ClinicConfig, Settings
from app.db.models import Consulta, LembreteEnviado, Mensagem, Paciente, StatusConsulta
from app.humanize import send_humanized
from app.llm import FALLBACK_REPLY, LLMAgent
from app.persona import build_system_prompt
from app.router import CLINICO, URGENTE, Router
from app.security import RateLimiter, mask_phone, normalize_phone
from app.tools import ToolContext, _titulo, alerta_urgente, chamar_humano, proxima_consulta, tools_for

log = logging.getLogger(__name__)

ATTACHMENT_PLACEHOLDER = "[O paciente enviou um anexo (áudio, imagem ou arquivo) sem texto]"


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(text.lower().strip().strip(".!").split())


def _conversation_labels(conv: dict) -> list[str]:
    return list(conv.get("labels") or [])


@dataclass
class Incoming:
    conversation_id: int
    phone: str
    name: str | None
    contact_id: int | None
    texts: list[str] = field(default_factory=list)


class Bot:
    def __init__(
        self,
        settings: Settings,
        clinic: ClinicConfig,
        sessionmaker: async_sessionmaker[AsyncSession],
        chatwoot: ChatwootClient,
        calendar: CalendarBackend,
        llm: LLMAgent,
        router: Router | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.s = settings
        self.clinic = clinic
        self.sessionmaker = sessionmaker
        self.chatwoot = chatwoot
        self.calendar = calendar
        self.llm = llm
        self.router = router or Router(llm.client, settings.router_model or settings.llm_model)
        self.sleep = sleep
        self.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
        self._pending: dict[int, Incoming] = {}
        self._timers: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    # --- entrada do webhook ---------------------------------------------
    async def handle_webhook(self, payload: dict[str, Any]) -> str:
        event = payload.get("event")
        if event == "conversation_status_changed":
            return await self._on_status_changed(payload)
        if event != "message_created":
            return "ignored:event"
        if payload.get("private"):
            return "ignored:private"

        conv = payload.get("conversation") or {}
        conv_id = conv.get("id")
        message_type = payload.get("message_type")
        sender = payload.get("sender") or {}
        contact = (conv.get("meta") or {}).get("sender") or {}

        if message_type in ("outgoing", 1):
            if sender.get("type") == "user" and payload.get("content"):
                await self._store_human_message(contact, payload["content"])
                return "stored:human"
            return "ignored:outgoing"
        if message_type not in ("incoming", 0) or conv_id is None:
            return "ignored:type"

        if conv.get("status") != "pending":
            return "ignored:not-pending"
        if HUMAN_LABEL in _conversation_labels(conv):
            return "ignored:human-label"

        phone = normalize_phone(contact.get("phone_number") or sender.get("phone_number"))
        if not phone:
            return "ignored:no-phone"
        if not self.rate_limiter.allow(phone):
            log.warning("Rate limit excedido para %s", mask_phone(phone))
            return "ignored:rate-limit"

        text = (payload.get("content") or "").strip() or ATTACHMENT_PLACEHOLDER
        item = self._pending.get(conv_id)
        if item is None:
            item = self._pending[conv_id] = Incoming(conv_id, phone, contact.get("name") or sender.get("name"),
                                                      contact.get("id") or sender.get("id"))
        item.texts.append(text)
        self._schedule(conv_id)
        return "queued"

    def _schedule(self, conv_id: int) -> None:
        timer = self._timers.get(conv_id)
        if timer and not timer.done():
            timer.cancel()
        self._timers[conv_id] = asyncio.create_task(self._debounced(conv_id))

    async def _debounced(self, conv_id: int) -> None:
        try:
            await asyncio.sleep(self.s.debounce_seconds)
        except asyncio.CancelledError:
            return
        lock = self._locks.setdefault(conv_id, asyncio.Lock())
        async with lock:
            item = self._pending.pop(conv_id, None)
            if item is None:
                return
            try:
                await self.process(item)
            except Exception:
                log.exception("Erro ao processar conversa %s", conv_id)

    async def drain(self) -> None:
        """Aguarda todos os debounces pendentes (usado nos testes e no shutdown)."""
        while self._timers:
            conv_id, task = self._timers.popitem()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _on_status_changed(self, payload: dict[str, Any]) -> str:
        conv = payload.get("conversation") or payload
        if conv.get("status") == "pending" and conv.get("id") and HUMAN_LABEL in _conversation_labels(conv):
            # Equipe devolveu a conversa ao bot: remove a etiqueta de atendimento humano.
            await self.chatwoot.remove_label(conv["id"], HUMAN_LABEL)
            return "returned-to-bot"
        return "ignored:status"

    async def _store_human_message(self, contact: dict, content: str) -> None:
        phone = normalize_phone(contact.get("phone_number"))
        if not phone:
            return
        async with self.sessionmaker() as session:
            paciente = await self._get_paciente(session, phone)
            if paciente:
                session.add(Mensagem(paciente_id=paciente.id, papel="humano", conteudo=content))
                await session.commit()

    # --- processamento ---------------------------------------------------
    @staticmethod
    async def _get_paciente(session: AsyncSession, phone: str) -> Paciente | None:
        return (await session.execute(select(Paciente).where(Paciente.telefone == phone))).scalar_one_or_none()

    async def _still_with_bot(self, conv_id: int) -> bool:
        """A equipe pode ter assumido durante o debounce."""
        try:
            conv = await self.chatwoot.get_conversation(conv_id)
        except Exception:
            return True
        return conv.get("status") == "pending" and HUMAN_LABEL not in _conversation_labels(conv)

    async def process(self, item: Incoming, now: datetime | None = None) -> str | None:
        now = now or datetime.now(timezone.utc)
        async with self.sessionmaker() as session:
            paciente = await self._get_paciente(session, item.phone)
            is_new = paciente is None
            if paciente is None:
                paciente = Paciente(telefone=item.phone)
                session.add(paciente)
            paciente.chatwoot_conversation_id = item.conversation_id
            if item.contact_id:
                paciente.chatwoot_contact_id = item.contact_id
            texto = "\n".join(item.texts)
            await session.flush()
            session.add(Mensagem(paciente_id=paciente.id, papel="paciente", conteudo=texto, criado_em=now))
            await session.commit()

            if not await self._still_with_bot(item.conversation_id):
                return None

            ctx = ToolContext(session=session, paciente=paciente, conversation_id=item.conversation_id,
                              chatwoot=self.chatwoot, calendar=self.calendar, clinic=self.clinic,
                              settings=self.s, now=now)

            categoria = None
            reply, hint = await self._button_reply(ctx, texto)
            if reply is None and hint is None:
                reply, categoria = await self._route(ctx, texto)
            if reply is None:
                reply = await self._llm_reply(ctx, hint, categoria)

            await self.chatwoot.mark_read(item.conversation_id)
            if is_new and self.clinic.privacidade.aviso_primeiro_contato:
                await self.chatwoot.send_message(item.conversation_id, self.clinic.privacidade.aviso_primeiro_contato)
                session.add(Mensagem(paciente_id=paciente.id, papel="bot",
                                     conteudo=self.clinic.privacidade.aviso_primeiro_contato))
            if reply:
                await send_humanized(item.conversation_id, reply, self.chatwoot.send_message,
                                     self.chatwoot.typing, self.sleep)
                session.add(Mensagem(paciente_id=paciente.id, papel="bot", conteudo=reply))
            await session.commit()
            return reply

    async def _route(self, ctx: ToolContext, texto: str) -> tuple[str | None, str]:
        """Router: casos clínicos/urgentes vão direto para a equipe com resposta fixa, sem IA."""
        history = await self._history(ctx)
        categoria = await self.router.classify(texto, history[:-1])
        log.info("Router: conversa %s -> %s", ctx.conversation_id, categoria)
        if categoria == URGENTE:
            await alerta_urgente(ctx, "Possível urgência (classificado automaticamente). Responder com prioridade.")
            return self.clinic.mensagens.urgente, categoria
        if categoria == CLINICO:
            await chamar_humano(ctx, "Assunto clínico (classificado automaticamente).")
            return self.clinic.mensagens.clinico, categoria
        return None, categoria

    async def _button_reply(self, ctx: ToolContext, texto: str) -> tuple[str | None, str | None]:
        """Trata os botões do template de lembrete (Confirmar / Remarcar)."""
        choice = _norm(texto)
        if choice not in ("confirmar", "remarcar"):
            return None, None
        consulta = await proxima_consulta(ctx)
        if consulta is None:
            return None, None
        lembrete = (await ctx.session.execute(
            select(LembreteEnviado).where(LembreteEnviado.consulta_id == consulta.id, LembreteEnviado.tipo == "d-3")
        )).scalar_one_or_none()
        if lembrete is None:
            return None, None

        quando = format_slot(consulta.inicio, ctx.zone)
        if choice == "remarcar":
            return None, (f"O paciente clicou em 'Remarcar' no lembrete da {consulta.tipo} de {quando}. "
                          "Consulte os horários e ofereça novas opções; depois use remarcar_consulta.")
        if consulta.status != StatusConsulta.CONFIRMADA:
            consulta.status = StatusConsulta.CONFIRMADA
            if consulta.google_event_id:
                try:
                    await ctx.calendar.update_event(
                        consulta.google_event_id,
                        summary=_titulo(consulta.tipo, ctx.paciente.nome or "", confirmada=True),
                    )
                except Exception:
                    log.exception("Não foi possível atualizar o título do evento")
            await ctx.session.commit()
        return f"Presença confirmada para {quando}. Agradecemos e até breve!", None

    async def _history(self, ctx: ToolContext) -> list[dict[str, str]]:
        stmt = (
            select(Mensagem).where(Mensagem.paciente_id == ctx.paciente.id)
            .order_by(Mensagem.criado_em.desc(), Mensagem.id.desc()).limit(self.s.history_limit)
        )
        msgs = reversed((await ctx.session.execute(stmt)).scalars().all())
        history = []
        for m in msgs:
            if m.papel == "paciente":
                history.append({"role": "user", "content": m.conteudo})
            elif m.papel == "humano":
                history.append({"role": "assistant", "content": f"[Equipe do consultório]: {m.conteudo}"})
            else:
                history.append({"role": "assistant", "content": m.conteudo})
        return history

    async def _llm_reply(self, ctx: ToolContext, hint: str | None, categoria: str | None) -> str:
        proxima: Consulta | None = await proxima_consulta(ctx)
        resumo = f"próximo agendamento: {proxima.tipo} em {format_slot(proxima.inicio, ctx.zone)} " \
                 f"(status {proxima.status})" if proxima else "nenhum agendamento futuro"
        system = build_system_prompt(self.clinic, ctx.now, ctx.zone, ctx.paciente.nome, resumo)
        if hint:
            system += f"\n\nOBSERVAÇÃO: {hint}"
        history = await self._history(ctx)
        try:
            tools = tools_for("acao" if hint else (categoria or "acao"))
            return await self.llm.reply(system, history, ctx, tools)
        except Exception:
            log.exception("Falha no LLM")
            if not ctx.handed_off:
                try:
                    await chamar_humano(ctx, "Falha técnica no assistente virtual")
                except Exception:
                    log.exception("Falha também no handoff")
            return FALLBACK_REPLY
