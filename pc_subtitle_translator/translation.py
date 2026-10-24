"""Streaming text translation over HTTPS; no remote response storage requested."""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx

from .protocol import ApiError, RESPONSES_URL, api_error, sse_events, translation_request
from .settings import Settings


class TextTranslator:
    def __init__(self, api_key: str, settings: Settings, client: httpx.AsyncClient):
        self.api_key = api_key
        self.settings = settings
        self.client = client

    async def translate(self, text: str, context: list[dict], on_delta: Callable[[str], None]) -> str:
        target = ""
        complete = False
        # Total deadline bounds even a connection that keeps sending empty events.
        async with asyncio.timeout(15):
            async with self.client.stream(
                "POST", RESPONSES_URL,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=translation_request(self.settings, text, context),
            ) as response:
                if response.status_code >= 300:
                    body = await response.aread()
                    try:
                        import json
                        payload = json.loads(body[:65536])
                    except (ValueError, UnicodeError):
                        payload = {"error": {"message": f"OpenAI translation request failed ({response.status_code})."}}
                    raise api_error(payload, self.api_key, response.status_code)
                async for event in sse_events(response.aiter_lines()):
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        target += str(event.get("delta", ""))
                        if len(target) > 16000:
                            raise ApiError("The translated subtitle exceeded the expected size.")
                        on_delta(target)
                    elif kind == "response.output_text.done":
                        final_text = event.get("text")
                        if isinstance(final_text, str):
                            target = final_text
                    elif kind == "response.refusal.delta" or kind == "response.refusal.done":
                        raise ApiError("This segment could not be translated by the selected model.", fatal=False)
                    elif kind in {"error", "response.failed"}:
                        payload = event.get("response", event)
                        raise api_error(payload, self.api_key)
                    elif kind == "response.incomplete":
                        raise ApiError("The translation ended before the subtitle was complete.", fatal=False)
                    elif kind == "response.completed":
                        state = event.get("response", {})
                        if state.get("status") not in {None, "completed"}:
                            raise ApiError("The translation did not complete.", fatal=False)
                        if not target:
                            for item in state.get("output", []):
                                for content in item.get("content", []):
                                    if content.get("type") == "output_text":
                                        target += content.get("text", "")
                        complete = True
                        break
        if not complete:
            raise ApiError("The translation connection ended before completion.", fatal=False)
        return target.strip()
