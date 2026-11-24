"""Telegram long polling. The bot token never goes into the logs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from cliphunter.jsonlog import log_event
from cliphunter.review.messages import build_caption, parse_callback, reason_keyboard, review_keyboard
from cliphunter.review.preview import preview_file
from cliphunter.review.state import TelegramState, load_state, save_state

ApplyAction = Callable[[int, str], Awaitable[None]]


class TelegramError(RuntimeError):
    pass


class HttpxTelegram:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._client = client
        self._owns = client is None

    async def aclose(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __call__(self, method: str, *, data: dict | None = None, json_body: dict | None = None, files: dict | None = None) -> dict:
        client = self._client
        if client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))
            client = self._client
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        opened = None
        request_files = None
        if files:
            path = files["video"]
            opened = path.open("rb")
            request_files = {"video": (path.name, opened, "video/mp4")}
        try:
            response = await client.post(url, data=data, json=json_body, files=request_files)
        finally:
            if opened is not None:
                opened.close()
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramError(f"respuesta no JSON ({response.status_code})") from exc
        if response.status_code >= 400 or not payload.get("ok"):
            description = payload.get("description") if isinstance(payload, dict) else "error"
            raise TelegramError(str(description))
        return payload


class TelegramNotifier:
    """Preferred review notifier. Ignores every chat except the one saved by telegram-setup."""

    def __init__(
        self,
        token: str,
        state_path: Path,
        apply_action: ApplyAction,
        transport: Callable[..., Awaitable[dict]] | None = None,
    ) -> None:
        self.state_path = state_path
        self.apply_action = apply_action
        self._transport = transport or HttpxTelegram(token)
        self._poll_timeout = 25 if transport is None else 0

    async def aclose(self) -> None:
        closer = getattr(self._transport, "aclose", None)
        if closer is not None:
            await closer()

    async def send(self, item: dict[str, Any]) -> None:
        state = load_state(self.state_path)
        if state.chat_id is None:
            raise TelegramError("todavía no hay chat id; ejecuta telegram-setup")
        path = await preview_file(Path(item["path"]))
        markup = json.dumps(review_keyboard(int(item["clip_id"])), ensure_ascii=False)
        await self._transport(
            "sendVideo",
            data={
                "chat_id": str(state.chat_id),
                "caption": build_caption(item),
                "reply_markup": markup,
            },
            files={"video": path},
        )

    async def run(self, stop: Any) -> None:
        import asyncio

        state = load_state(self.state_path)
        offset = state.offset
        while not stop.is_set():
            try:
                payload = await self._transport(
                    "getUpdates",
                    json_body={
                        "offset": offset,
                        "timeout": self._poll_timeout,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in payload.get("result") or []:
                    offset = int(update["update_id"]) + 1
                    state.offset = offset
                    save_state(self.state_path, state)
                    await self._handle(update, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event("WARNING", "telegram.poll_failed", error=str(exc))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    continue

    async def _handle(self, update: dict, state: TelegramState) -> None:
        if state.chat_id is None:
            state = load_state(self.state_path)
        query = update.get("callback_query")
        if not query:
            return
        query_id = query.get("id")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if state.chat_id is None or chat_id != state.chat_id:
            await self._answer(query_id, "Este chat no está autorizado.")
            log_event("WARNING", "telegram.rejected_chat", chat_id=chat_id)
            return
        parsed = parse_callback(str(query.get("data") or ""))
        if parsed is None:
            await self._answer(query_id, "Acción desconocida.")
            return
        code, clip_id = parsed
        if code == "ask":
            await self._reasons(int(state.chat_id), clip_id)
            await self._answer(query_id, "Elige el motivo.")
            return
        try:
            await self.apply_action(clip_id, code)
            await self._answer(query_id, "Hecho.")
        except Exception as exc:
            log_event("ERROR", "telegram.action_failed", error=str(exc), clip_id=clip_id)
            await self._answer(query_id, "No se pudo aplicar.")

    async def _reasons(self, chat_id: int, clip_id: int) -> None:
        await self._transport(
            "sendMessage",
            json_body={
                "chat_id": str(chat_id),
                "text": "¿Por qué lo rechazas?",
                "reply_markup": reason_keyboard(clip_id),
            },
        )

    async def _answer(self, query_id: str | None, text: str) -> None:
        if not query_id:
            return
        try:
            await self._transport("answerCallbackQuery", json_body={"callback_query_id": query_id, "text": text})
        except Exception as exc:
            log_event("WARNING", "telegram.answer_failed", error=str(exc))


async def wait_for_start(transport: Callable[..., Awaitable[dict]], state_path: Path) -> int:
    """Block until the allowed user sends /start. Used by ``telegram-setup``."""

    state = load_state(state_path)
    offset = state.offset
    while True:
        payload = await transport(
            "getUpdates",
            json_body={"offset": offset, "timeout": 0, "allowed_updates": ["message"]},
        )
        updates = payload.get("result") or []
        if not updates:
            await asyncio.sleep(1)
            continue
        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or {}
            text = str(message.get("text") or "").strip()
            chat = message.get("chat") or {}
            if text.startswith("/start") and chat.get("id") is not None:
                state.chat_id = int(chat["id"])
                user = message.get("from") or {}
                state.username = chat.get("username") or user.get("username")
                state.offset = offset
                save_state(state_path, state)
                return state.chat_id
        state.offset = offset
        save_state(state_path, state)
