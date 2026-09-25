"""Rotina diária (09:00 America/Sao_Paulo): lembretes D-3, pós-consulta D+3 e limpeza LGPD."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.calendar_service import CalendarBackend, day_bounds
from app.chatwoot import HUMAN_LABEL, ChatwootClient
from app.config import ClinicConfig, Settings
from app.db.models import Consulta, LembreteEnviado, Mensagem, Paciente, StatusConsulta
from app.security import mask_phone

log = logging.getLogger(__name__)

D_MINUS = "d-3"
D_PLUS = "d+3"

_PHONE_IN_TEXT = re.compile(r"(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9?\d{4}[\s-]?\d{4}")


def extract_phone(event: dict) -> str | None:
    private = (event.get("extendedProperties") or {}).get("private") or {}
    raw = private.get("telefone")
    if not raw:
        for text in (event.get("description") or "", event.get("summary") or ""):
            match = _PHONE_IN_TEXT.search(text)
            if match:
                raw = match.group(0)
                break
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) in (10, 11):
        digits = "55" + digits
    return f"+{digits}" if len(digits) in (12, 13) else None


def _event_start(event: dict) -> datetime | None:
    value = (event.get("start") or {}).get("dateTime")
    return datetime.fromisoformat(value) if value else None


def _event_end(event: dict) -> datetime | None:
    value = (event.get("end") or {}).get("dateTime")
    return datetime.fromisoformat(value) if value else None


def _looks_like_appointment(event: dict) -> bool:
    title = (event.get("summary") or "").replace("✅", "").strip().lower()
    return title.startswith(("consulta", "retorno"))


def _nome_evento(event: dict) -> str | None:
    title = (event.get("summary") or "").replace("✅", "").strip()
    for sep in ("—", "-", ":"):
        if sep in title:
            return title.split(sep, 1)[1].strip() or None
    return None


@dataclass
class Scheduler:
    settings: Settings
    clinic: ClinicConfig
    sessionmaker: async_sessionmaker[AsyncSession]
    chatwoot: ChatwootClient
    calendar: CalendarBackend

    @property
    def zone(self):
        return self.settings.zone

    async def run_daily(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        summary: dict[str, int] = {}
        async with self.sessionmaker() as session:
            summary["realizadas"] = await self.mark_done(session, now)
            summary["lembretes_d3"] = await self.send_before(session, now)
            summary["pos_consulta_d3"] = await self.send_after(session, now)
            summary["mensagens_removidas"] = await self.cleanup(session, now)
        log.info("Rotina diária concluída: %s", summary)
        return summary

    async def mark_done(self, session: AsyncSession, now: datetime) -> int:
        rows = (await session.execute(
            select(Consulta).where(Consulta.status.in_(StatusConsulta.ATIVAS), Consulta.fim <= now)
        )).scalars().all()
        for c in rows:
            c.status = StatusConsulta.REALIZADA
        await session.commit()
        return len(rows)

    # --- D-3 -------------------------------------------------------------
    async def _sync_day_with_calendar(self, session: AsyncSession, day_start: datetime, day_end: datetime) -> None:
        """Importa consultas marcadas direto na agenda (com telefone) e detecta cancelamentos/mudanças."""
        events = [e for e in await self.calendar.list_events(day_start, day_end) if e.get("status") != "cancelled"]
        by_id = {e["id"]: e for e in events}

        known = (await session.execute(
            select(Consulta).where(Consulta.status.in_(StatusConsulta.ATIVAS),
                                   Consulta.inicio >= day_start, Consulta.inicio < day_end)
        )).scalars().all()
        for c in known:
            if not c.google_event_id or c.google_event_id in by_id:
                continue
            try:
                ev = await self.calendar.get_event(c.google_event_id)
            except Exception:
                ev = None
            if ev is None or ev.get("status") == "cancelled":
                log.info("Evento da consulta %s foi removido da agenda; marcando como cancelada", c.id)
                c.status = StatusConsulta.CANCELADA
            elif (start := _event_start(ev)) and (end := _event_end(ev)):
                c.inicio, c.fim = start, end  # equipe mudou o horário direto na agenda

        linked = set((await session.execute(
            select(Consulta.google_event_id).where(Consulta.google_event_id.in_(list(by_id) or [""]))
        )).scalars().all())
        for ev in events:
            if ev["id"] in linked or not _looks_like_appointment(ev):
                continue
            start, end = _event_start(ev), _event_end(ev)
            phone = extract_phone(ev)
            if not phone or not start or not end:
                log.warning("Consulta marcada por fora sem telefone identificável (evento %s); sem lembrete", ev["id"])
                continue
            paciente = (await session.execute(select(Paciente).where(Paciente.telefone == phone))).scalar_one_or_none()
            if paciente is None:
                paciente = Paciente(telefone=phone, nome=_nome_evento(ev))
                session.add(paciente)
                await session.flush()
            tipo = "retorno" if (ev.get("summary") or "").lower().lstrip("✅ ").startswith("retorno") else "consulta"
            session.add(Consulta(paciente_id=paciente.id, tipo=tipo, inicio=start, fim=end,
                                 status=StatusConsulta.AGENDADA, google_event_id=ev["id"], origem="agenda"))
        await session.commit()

    async def send_before(self, session: AsyncSession, now: datetime) -> int:
        target = now.astimezone(self.zone).date() + timedelta(days=self.clinic.lembretes.dias_antes)
        start, end = day_bounds(target, self.zone)
        try:
            await self._sync_day_with_calendar(session, start, end)
        except Exception:
            await session.rollback()
            log.exception("Falha ao sincronizar a agenda; seguindo só com o banco")

        consultas = (await session.execute(
            select(Consulta).options(selectinload(Consulta.paciente))
            .where(Consulta.status.in_(StatusConsulta.ATIVAS), Consulta.inicio >= start, Consulta.inicio < end)
        )).scalars().all()
        sent = 0
        for c in consultas:
            local = c.inicio.astimezone(self.zone)
            nome = c.paciente.nome or "paciente"
            params = {"1": nome, "2": local.strftime("%d/%m/%Y"), "3": local.strftime("%H:%M")}
            content = (f"Olá, {nome}. Lembramos da sua {c.tipo} com a {self.clinic.medica} no dia "
                       f"{params['2']}, às {params['3']}. Por gentileza, confirme sua presença.")
            if await self._send_once(session, c, D_MINUS, self.clinic.templates.lembrete_consulta, params, content):
                sent += 1
        return sent

    # --- D+3 -------------------------------------------------------------
    async def send_after(self, session: AsyncSession, now: datetime) -> int:
        target = now.astimezone(self.zone).date() - timedelta(days=self.clinic.lembretes.dias_depois)
        start, end = day_bounds(target, self.zone)
        consultas = (await session.execute(
            select(Consulta).options(selectinload(Consulta.paciente))
            .where(Consulta.status == StatusConsulta.REALIZADA, Consulta.inicio >= start, Consulta.inicio < end)
        )).scalars().all()
        sent = 0
        for c in consultas:
            nome = c.paciente.nome or "paciente"
            content = (f"Olá, {nome}. A {self.clinic.medica} gostaria de saber se está tudo bem após a sua "
                       "consulta. Caso tenha alguma dúvida, estamos à disposição.")
            if await self._send_once(session, c, D_PLUS, self.clinic.templates.pos_consulta, {"1": nome}, content):
                sent += 1
        return sent

    async def _send_once(self, session: AsyncSession, consulta: Consulta, tipo: str, template: str,
                         params: dict[str, str], content: str) -> bool:
        paciente = consulta.paciente
        registro = LembreteEnviado(consulta_id=consulta.id, tipo=tipo)
        session.add(registro)
        try:
            await session.commit()  # a chave única (consulta_id, tipo) garante envio único
        except IntegrityError:
            await session.rollback()
            return False
        try:
            conv_id = paciente.chatwoot_conversation_id
            if conv_id is None:
                contact_id, conv_id = await self.chatwoot.ensure_conversation(paciente.telefone, paciente.nome)
                paciente.chatwoot_contact_id, paciente.chatwoot_conversation_id = contact_id, conv_id
            await self.chatwoot.send_template(conv_id, template, self.clinic.templates.idioma, params, content)
            # Garante que a resposta do paciente seja tratada pelo bot (exceto se a equipe estiver atendendo).
            try:
                if HUMAN_LABEL not in await self.chatwoot.get_labels(conv_id):
                    await self.chatwoot.set_status(conv_id, "pending")
            except Exception:
                log.warning("Não foi possível ajustar o status da conversa %s", conv_id)
            session.add(Mensagem(paciente_id=paciente.id, papel="bot", conteudo=content))
            await session.commit()
            log.info("Lembrete %s enviado (consulta %s, %s)", tipo, consulta.id, mask_phone(paciente.telefone))
            return True
        except Exception:
            log.exception("Falha ao enviar lembrete %s da consulta %s", tipo, consulta.id)
            await session.rollback()
            await session.delete(await session.merge(registro))
            await session.commit()
            return False

    # --- LGPD ------------------------------------------------------------
    async def cleanup(self, session: AsyncSession, now: datetime) -> int:
        limit = now - timedelta(days=self.clinic.privacidade.retencao_mensagens_dias)
        result = await session.execute(delete(Mensagem).where(Mensagem.criado_em < limit))
        await session.commit()
        return result.rowcount or 0


def start_internal_scheduler(job, clinic: ClinicConfig, tz: str):
    """APScheduler rodando dentro do processo do bot, todo dia no horário do clinic.yaml."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    hour, minute = (int(x) for x in clinic.lembretes.horario_execucao.split(":"))
    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(job, CronTrigger(hour=hour, minute=minute, timezone=tz), id="daily",
                  misfire_grace_time=3600, coalesce=True, max_instances=1)
    sched.start()
    return sched
