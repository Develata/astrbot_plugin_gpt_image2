from __future__ import annotations

from typing import Any

from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp


class AstrBotMessageSender:
    def __init__(self, context: Any) -> None:
        self.context = context

    async def send_text(self, session: str, text: str) -> None:
        await self.context.send_message(session, MessageChain([Comp.Plain(text)]))

    async def send_image(self, session: str, path_or_url: str, caption: str | None = None) -> None:
        chain = []
        if caption:
            chain.append(Comp.Plain(caption + "\n"))
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            chain.append(Comp.Image.fromURL(path_or_url))
        else:
            chain.append(Comp.Image.fromFileSystem(path_or_url))
        await self.context.send_message(session, MessageChain(chain))
