"""Deixa as respostas com cara de conversa: quebra em até 3 mensagens e espera entre elas."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

MAX_PARTS = 3


def split_messages(text: str, max_parts: int = MAX_PARTS) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if len(parts) <= max_parts:
        return parts
    return parts[: max_parts - 1] + ["\n\n".join(parts[max_parts - 1:])]


def typing_delay(text: str) -> float:
    return min(1.5 + len(text) / 40, 6.0)


async def send_humanized(
    conversation_id: int,
    text: str,
    send: Callable[[int, str], Awaitable[object]],
    typing: Callable[[int, bool], Awaitable[object]] | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> list[str]:
    parts = split_messages(text)
    for part in parts:
        if typing:
            await typing(conversation_id, True)
        await sleep(typing_delay(part))
        await send(conversation_id, part)
    if typing and parts:
        await typing(conversation_id, False)
    return parts
