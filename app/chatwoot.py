"""Cliente mínimo da API do Chatwoot.

Dois tokens:
- CHATWOOT_BOT_TOKEN: token do Agent Bot; usado para enviar mensagens (aparecem como do bot).
- CHATWOOT_API_TOKEN: token de um usuário administrador; usado para etiquetas, prioridade,
  contatos e criação de conversas (endpoints que o Agent Bot não pode chamar).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

HUMAN_LABEL = "atendimento-humano"
URGENT_LABEL = "urgente"


class ChatwootClient:
    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings or get_settings()
        self.http = http or httpx.AsyncClient(timeout=20)
        self.base = f"{self.s.chatwoot_url.rstrip('/')}/api/v1/accounts/{self.s.chatwoot_account_id}"

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _req(self, method: str, path: str, *, bot: bool = False, **kw: Any) -> Any:
        token = self.s.chatwoot_bot_token if bot and self.s.chatwoot_bot_token else self.s.chatwoot_api_token
        resp = await self.http.request(method, f"{self.base}{path}", headers={"api_access_token": token}, **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    # --- mensagens -------------------------------------------------------
    async def send_message(self, conversation_id: int, content: str, private: bool = False) -> Any:
        body = {"content": content, "message_type": "outgoing", "private": private}
        return await self._req("POST", f"/conversations/{conversation_id}/messages", bot=True, json=body)

    async def send_template(
        self, conversation_id: int, name: str, language: str, params: dict[str, str], content: str
    ) -> Any:
        body = {
            "content": content,
            "message_type": "outgoing",
            "template_params": {
                "name": name,
                "category": "UTILITY",
                "language": language,
                "processed_params": params,
            },
        }
        return await self._req("POST", f"/conversations/{conversation_id}/messages", bot=True, json=body)

    async def typing(self, conversation_id: int, on: bool) -> None:
        try:
            await self._req(
                "POST",
                f"/conversations/{conversation_id}/toggle_typing_status",
                bot=True,
                json={"typing_status": "on" if on else "off"},
            )
        except httpx.HTTPError:
            pass  # indicador de "digitando" é opcional

    async def mark_read(self, conversation_id: int) -> None:
        try:
            await self._req("POST", f"/conversations/{conversation_id}/update_last_seen", bot=True)
        except httpx.HTTPError:
            pass

    # --- estado da conversa ---------------------------------------------
    async def set_status(self, conversation_id: int, status: str) -> Any:
        return await self._req(
            "POST", f"/conversations/{conversation_id}/toggle_status", bot=True, json={"status": status}
        )

    async def set_priority(self, conversation_id: int, priority: str) -> Any:
        return await self._req(
            "POST", f"/conversations/{conversation_id}/toggle_priority", json={"priority": priority}
        )

    async def get_labels(self, conversation_id: int) -> list[str]:
        data = await self._req("GET", f"/conversations/{conversation_id}/labels")
        return list((data or {}).get("payload", []))

    async def add_labels(self, conversation_id: int, labels: list[str]) -> None:
        # A API substitui a lista inteira, então mesclamos com as atuais.
        current = await self.get_labels(conversation_id)
        merged = sorted(set(current) | set(labels))
        await self._req("POST", f"/conversations/{conversation_id}/labels", json={"labels": merged})

    async def remove_label(self, conversation_id: int, label: str) -> None:
        current = await self.get_labels(conversation_id)
        if label in current:
            remaining = [x for x in current if x != label]
            await self._req("POST", f"/conversations/{conversation_id}/labels", json={"labels": remaining})

    async def get_conversation(self, conversation_id: int) -> dict:
        return await self._req("GET", f"/conversations/{conversation_id}")

    # --- contatos / conversas (lembretes para quem não tem conversa aberta) ---
    async def ensure_conversation(self, phone: str, name: str | None = None) -> tuple[int, int]:
        """Retorna (contact_id, conversation_id) para o telefone na inbox do WhatsApp."""
        inbox_id = self.s.chatwoot_inbox_id
        found = await self._req("GET", "/contacts/search", params={"q": phone})
        contacts = (found or {}).get("payload", [])
        contact = next((c for c in contacts if c.get("phone_number") == phone), None)
        if contact is None:
            created = await self._req(
                "POST", "/contacts", json={"inbox_id": inbox_id, "name": name or phone, "phone_number": phone}
            )
            contact = created["payload"]["contact"] if "contact" in created.get("payload", {}) else created["payload"]
        contact_id = contact["id"]

        convs = await self._req("GET", f"/contacts/{contact_id}/conversations")
        for conv in (convs or {}).get("payload", []):
            if conv.get("inbox_id") == inbox_id:
                return contact_id, conv["id"]

        source_id = None
        for ci in contact.get("contact_inboxes", []) or []:
            if (ci.get("inbox") or {}).get("id") == inbox_id:
                source_id = ci.get("source_id")
        if source_id is None:
            ci = await self._req(
                "POST", f"/contacts/{contact_id}/contact_inboxes", json={"inbox_id": inbox_id}
            )
            source_id = ci["source_id"]
        conv = await self._req(
            "POST",
            "/conversations",
            json={"source_id": source_id, "inbox_id": inbox_id, "contact_id": contact_id, "status": "pending"},
        )
        return contact_id, conv["id"]
