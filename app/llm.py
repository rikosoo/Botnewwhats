"""Cliente LLM compatível com OpenAI (Gemini ou Groq) e loop de tool calling."""

from __future__ import annotations

import logging
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings, get_settings
from app.tools import TOOL_DEFINITIONS, ToolContext, chamar_humano, execute_tool

log = logging.getLogger(__name__)

FALLBACK_REPLY = (
    "Peço desculpas, não consegui concluir sua solicitação agora. "
    "Vou encaminhar sua mensagem para a nossa equipe, que retornará em breve."
)


def make_client(settings: Settings) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.resolved_llm_base_url, timeout=60)


class LLMAgent:
    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        self.s = settings or get_settings()
        self.client = client or make_client(self.s)

    async def reply(self, system_prompt: str, history: list[dict[str, str]], ctx: ToolContext) -> str:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}, *history]
        for _ in range(self.s.max_tool_iterations):
            resp = await self.client.chat.completions.create(
                model=self.s.llm_model,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                temperature=0.3,
            )
            msg = resp.choices[0].message
            tool_calls = list(msg.tool_calls or [])
            if not tool_calls:
                return (msg.content or "").strip()

            messages.append({
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                log.info("Tool call: %s", tc.function.name)
                result = await execute_tool(ctx, tc.function.name, tc.function.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        log.warning("Limite de %s iterações de tool calling atingido", self.s.max_tool_iterations)
        if not ctx.handed_off:
            await chamar_humano(ctx, "Limite de iterações do assistente atingido")
        return FALLBACK_REPLY
