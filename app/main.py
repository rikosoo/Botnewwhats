from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, Request

from app.calendar_service import GoogleCalendar
from app.chatwoot import ChatwootClient
from app.config import get_clinic, get_settings
from app.conversation import Bot
from app.db.session import dispose_engine, get_sessionmaker
from app.llm import LLMAgent
from app.scheduler import Scheduler, start_internal_scheduler
from app.security import setup_logging, verify_scheduler_token, verify_webhook_token

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    clinic = get_clinic()
    sessionmaker = get_sessionmaker()
    chatwoot = ChatwootClient(settings)
    calendar = GoogleCalendar(settings)
    app.state.bot = Bot(settings, clinic, sessionmaker, chatwoot, calendar, LLMAgent(settings))
    app.state.scheduler = Scheduler(settings, clinic, sessionmaker, chatwoot, calendar)
    sched = None
    if settings.enable_internal_scheduler:
        sched = start_internal_scheduler(app.state.scheduler.run_daily, clinic, settings.tz)
        log.info("Agendador interno ativo (%s)", clinic.lembretes.horario_execucao)
    yield
    if sched:
        sched.shutdown(wait=False)
    await app.state.bot.drain()
    await chatwoot.aclose()
    await dispose_engine()


app = FastAPI(title="Bot WhatsApp — Dra. Ana Paula", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/chatwoot", dependencies=[Depends(verify_webhook_token)])
async def chatwoot_webhook(request: Request) -> dict:
    payload = await request.json()
    result = await request.app.state.bot.handle_webhook(payload)
    return {"result": result}


@app.post("/jobs/daily", dependencies=[Depends(verify_scheduler_token)])
async def run_daily_job(request: Request, background: BackgroundTasks) -> dict:
    """Alternativa ao agendador interno (ex.: EventBridge Scheduler / cron chamando este endpoint)."""
    background.add_task(request.app.state.scheduler.run_daily)
    return {"status": "started"}
