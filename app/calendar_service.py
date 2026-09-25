"""Google Agenda (Calendar API v3 via REST + service account) e cálculo de horários livres.

Tudo que está na agenda é tratado como ocupado (inclusive bloqueios feitos pela equipe).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.config import ClinicConfig, Settings, get_settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
API = "https://www.googleapis.com/calendar/v3"

Interval = tuple[datetime, datetime]


class CalendarBackend(Protocol):
    async def busy(self, start: datetime, end: datetime) -> list[Interval]: ...
    async def create_event(self, summary: str, description: str, start: datetime, end: datetime,
                           private: dict[str, str]) -> str: ...
    async def update_event(self, event_id: str, **fields: Any) -> None: ...
    async def delete_event(self, event_id: str) -> None: ...
    async def list_events(self, start: datetime, end: datetime) -> list[dict]: ...
    async def get_event(self, event_id: str) -> dict: ...


class GoogleCalendar:
    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> None:
        from google.oauth2 import service_account  # import tardio: testes não precisam

        self.s = settings or get_settings()
        self.calendar_id = self.s.google_calendar_id
        self.http = http or httpx.AsyncClient(timeout=20)
        self._creds = service_account.Credentials.from_service_account_info(
            self.s.google_credentials_info(), scopes=SCOPES
        )
        self._lock = asyncio.Lock()

    async def _token(self) -> str:
        async with self._lock:
            if not self._creds.valid:
                from google.auth.transport.requests import Request

                await asyncio.to_thread(self._creds.refresh, Request())
            return self._creds.token

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        resp = await self.http.request(method, f"{API}{path}", headers=headers, **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    @property
    def _events_path(self) -> str:
        return f"/calendars/{quote(self.calendar_id, safe='')}/events"

    async def busy(self, start: datetime, end: datetime) -> list[Interval]:
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": self.calendar_id}]}
        data = await self._req("POST", "/freeBusy", json=body)
        cal = data["calendars"][self.calendar_id]
        if cal.get("errors"):
            raise RuntimeError(f"Erro no free/busy do Google Agenda: {cal['errors']}")
        return [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in cal["busy"]]

    async def create_event(self, summary: str, description: str, start: datetime, end: datetime,
                           private: dict[str, str]) -> str:
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": self.s.tz},
            "end": {"dateTime": end.isoformat(), "timeZone": self.s.tz},
            "extendedProperties": {"private": private},
        }
        data = await self._req("POST", self._events_path, json=body)
        return data["id"]

    async def update_event(self, event_id: str, **fields: Any) -> None:
        body: dict[str, Any] = {}
        if "summary" in fields:
            body["summary"] = fields["summary"]
        if "start" in fields:
            body["start"] = {"dateTime": fields["start"].isoformat(), "timeZone": self.s.tz}
        if "end" in fields:
            body["end"] = {"dateTime": fields["end"].isoformat(), "timeZone": self.s.tz}
        if "private" in fields:
            body["extendedProperties"] = {"private": fields["private"]}
        await self._req("PATCH", f"{self._events_path}/{quote(event_id, safe='')}", json=body)

    async def delete_event(self, event_id: str) -> None:
        try:
            await self._req("DELETE", f"{self._events_path}/{quote(event_id, safe='')}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404, 410):
                raise

    async def get_event(self, event_id: str) -> dict:
        return await self._req("GET", f"{self._events_path}/{quote(event_id, safe='')}")

    async def list_events(self, start: datetime, end: datetime) -> list[dict]:
        items: list[dict] = []
        params: dict[str, Any] = {
            "timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
        }
        while True:
            data = await self._req("GET", self._events_path, params=params)
            items.extend(data.get("items", []))
            if not data.get("nextPageToken"):
                return items
            params["pageToken"] = data["nextPageToken"]


# --- cálculo de horários -------------------------------------------------

def _overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def candidate_slots(clinic: ClinicConfig, tipo: str, first_day: date, last_day: date,
                    zone: ZoneInfo) -> list[Interval]:
    """Todos os horários de início possíveis pelas janelas do clinic.yaml (ainda sem free/busy)."""
    dur = timedelta(minutes=clinic.duracao(tipo))
    slots: list[Interval] = []
    day = first_day
    while day <= last_day:
        for w_start, w_end in clinic.atendimento.windows_for(day.weekday()):
            cursor = datetime.combine(day, w_start, tzinfo=zone)
            limit = datetime.combine(day, w_end, tzinfo=zone)
            while cursor + dur <= limit:
                slots.append((cursor, cursor + dur))
                cursor += dur
        day += timedelta(days=1)
    return slots


def booking_bounds(clinic: ClinicConfig, now: datetime) -> tuple[datetime, datetime]:
    earliest = now + timedelta(hours=clinic.atendimento.antecedencia_minima_horas)
    latest = now + timedelta(days=clinic.atendimento.janela_maxima_dias)
    return earliest, latest


def free_slots(clinic: ClinicConfig, tipo: str, first_day: date, last_day: date, busy: list[Interval],
               now: datetime, zone: ZoneInfo, not_after: datetime | None = None) -> list[Interval]:
    earliest, latest = booking_bounds(clinic, now)
    if not_after is not None:
        latest = min(latest, not_after)
    result = []
    for slot in candidate_slots(clinic, tipo, first_day, last_day, zone):
        if slot[0] < earliest or slot[0] > latest:
            continue
        if any(_overlaps(slot, b) for b in busy):
            continue
        result.append(slot)
    return result


def is_bookable(clinic: ClinicConfig, tipo: str, start: datetime, busy: list[Interval], now: datetime,
                zone: ZoneInfo, not_after: datetime | None = None) -> bool:
    local_day = start.astimezone(zone).date()
    return any(s[0] == start for s in free_slots(clinic, tipo, local_day, local_day, busy, now, zone, not_after))


def parse_local(value: str, zone: ZoneInfo) -> datetime:
    """Aceita '2026-10-06T14:00', '2026-10-06 14:00' ou ISO com fuso."""
    dt = datetime.fromisoformat(value.strip().replace(" ", "T"))
    return dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)


def parse_day(value: str) -> date:
    return date.fromisoformat(value.strip()[:10])


WEEKDAY_NAMES = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira",
                 "sábado", "domingo"]


def format_slot(start: datetime, zone: ZoneInfo) -> str:
    local = start.astimezone(zone)
    return f"{WEEKDAY_NAMES[local.weekday()]}, {local:%d/%m} às {local:%H:%M}"


def day_bounds(day: date, zone: ZoneInfo) -> Interval:
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)
