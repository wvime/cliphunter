"""Local fallback: a localhost page with the same review buttons, plus a toast."""

from __future__ import annotations

import asyncio
from typing import Any

from cliphunter.hotkey import notify_user
from cliphunter.jsonlog import log_event

_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClipHunter</title>
<style>
  body {{ font-family: sans-serif; margin: 1.5rem; background: #111; color: #eee; }}
  h1 {{ font-size: 1.3rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }}
  article {{ background: #1c1c1c; padding: 0.8rem; border-radius: 8px; }}
  video {{ width: 100%; background: #000; }}
  button {{ margin: 0.2rem 0.2rem 0 0; }}
  pre {{ white-space: pre-wrap; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>Clips pendientes</h1>
<p>{intro}</p>
<div class="grid">
{cards}
</div>
</body>
</html>
"""


def render_page(items: list[dict[str, Any]]) -> str:
    if not items:
        cards = "<p>No hay clips pendientes.</p>"
    else:
        cards = "\n".join(_card(item) for item in items)
    intro = "Misma cola que Telegram. Solo esta máquina puede abrir la página."
    return _PAGE.format(intro=intro, cards=cards)


def _card(item: dict[str, Any]) -> str:
    from cliphunter.review.messages import build_caption

    clip_id = int(item["clip_id"])
    caption = build_caption(item).replace("&", "&amp;").replace("<", "&lt;")
    buttons = [
        ("a", "Aprobar"),
        ("r", "Rechazar"),
        ("is", "Ajustar -5s inicio"),
        ("ie", "Ajustar +5s inicio"),
        ("fs", "Ajustar -5s fin"),
        ("fe", "Ajustar +5s fin"),
    ]
    forms = []
    for code, label in buttons:
        forms.append(
            "<form method=\"post\" action=\"/decision\" style=\"display:inline\">"
            f"<input type=\"hidden\" name=\"clip_id\" value=\"{clip_id}\">"
            f"<button name=\"op\" value=\"{code}\" type=\"submit\">{label}</button>"
            "</form>"
        )
    return (
        "<article>"
        f"<video src=\"/media/{clip_id}\" controls></video>"
        f"<pre>{caption}</pre>"
        f"{''.join(forms)}"
        "</article>"
    )


def build_review_app(runtime: Any):
    from fastapi import FastAPI, Form
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

    app = FastAPI(title="ClipHunter review")

    @app.get("/")
    async def home() -> HTMLResponse:
        items = await runtime.pending_items()
        return HTMLResponse(render_page(items))

    @app.post("/decision")
    async def decision(clip_id: int = Form(...), op: str = Form(...)) -> RedirectResponse:
        try:
            await runtime.apply_code(clip_id, op)
        except Exception as exc:
            log_event("ERROR", "review.local_action_failed", error=str(exc), clip_id=clip_id)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/media/{clip_id}")
    async def media(clip_id: int):
        path = await runtime.media_path(clip_id)
        if path is None or not path.exists():
            return HTMLResponse("no está el archivo", status_code=404)
        return FileResponse(path)

    return app


class LocalNotifier:
    """Toast plus a small page on 127.0.0.1. Never required for recording."""

    def __init__(self, runtime: Any, port: int) -> None:
        self.runtime = runtime
        self.port = port
        self._seen: set[int] = set()
        self._server: Any = None

    async def send(self, item: dict[str, Any]) -> None:
        self.announce(item)

    async def run(self, stop: asyncio.Event) -> None:
        await self.serve(stop)

    def announce(self, item: dict[str, Any]) -> None:
        clip_id = int(item["clip_id"])
        if clip_id in self._seen:
            return
        self._seen.add(clip_id)
        try:
            notify_user("ClipHunter", f"Clip pendiente #{clip_id} de {item.get('channel') or ''}".strip())
        except Exception as exc:
            log_event("WARNING", "review.toast_failed", error=str(exc))

    async def serve(self, stop: asyncio.Event) -> None:
        try:
            import uvicorn
        except ImportError:
            log_event("WARNING", "review.local_unavailable", reason="falta uvicorn")
            await stop.wait()
            return
        try:
            app = build_review_app(self.runtime)
            config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
            server = uvicorn.Server(config)
            self._server = server
        except Exception as exc:
            log_event("WARNING", "review.local_unavailable", error=str(exc))
            await stop.wait()
            return

        async def _stop_when_asked() -> None:
            await stop.wait()
            server.should_exit = True

        watcher = asyncio.create_task(_stop_when_asked())
        try:
            await server.serve()
        except Exception as exc:
            log_event("WARNING", "review.local_failed", error=str(exc))
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
