"""Command line: ``run``, ``clip``, ``doctor``, ``init-db``."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from cliphunter import __version__
from cliphunter.app import prepare_run, run_app
from cliphunter.authprompt import AuthorizationRequired
from cliphunter.calibrate import calibrate_command
from cliphunter.clip import (
    ClipError,
    create_clip,
    default_clip_path,
    find_channel,
    index_path_for,
)
from cliphunter.config import ConfigError, channel_key, load_config
from cliphunter.env import load_env
from cliphunter.jsonlog import log_event
from cliphunter.replay import replay_command
from cliphunter.storage.repo import SqliteRepo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cliphunter",
        description="Vigila directos y corta clips solo cuando el momento lo merece.",
    )
    parser.add_argument("--version", action="store_true", help="Muestra la versión y sale.")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Vigila los canales autorizados y graba el buffer.")
    run.add_argument("--config", default="config.yaml", help="Ruta del YAML. Por defecto config.yaml.")
    run.add_argument(
        "--yes",
        action="store_true",
        help="No pregunta autorización. Para tareas no interactivas: confirma los canales con authorized: true.",
    )
    run.add_argument("--env", default=".env", help="Archivo .env con las claves. Por defecto .env.")
    run.add_argument(
        "--no-hotkey",
        action="store_true",
        help="No registra el atajo global (por defecto Ctrl+Alt+C).",
    )

    clip = sub.add_parser("clip", help="Corta los últimos N segundos del buffer de un canal.")
    clip.add_argument("--canal", required=True, help="Handle o clave del canal, por ejemplo TuCanal.")
    clip.add_argument("--ultimos", required=True, type=float, help="Segundos hacia atrás desde el último segmento cerrado.")
    clip.add_argument("--config", default="config.yaml", help="Ruta del YAML. Por defecto config.yaml.")
    clip.add_argument("--output", default=None, help="Ruta del mp4. Por defecto en la carpeta clips.")

    init_db = sub.add_parser("init-db", help="Crea el SQLite si todavía no existe.")
    init_db.add_argument("--config", default="config.yaml")

    setup = sub.add_parser(
        "telegram-setup",
        help="Espera un /start de @clipowii_bot y guarda el chat id permitido.",
    )
    setup.add_argument("--config", default="config.yaml")
    setup.add_argument("--env", default=".env")

    sub.add_parser("doctor", help="Comprueba Python, ffmpeg y streamlink.")

    replay = sub.add_parser("replay", help="Puntúa un VOD con un volcado de chat.")
    replay.add_argument("archivo", nargs="?", default=None, help="Vídeo ya grabado, por ejemplo archivo.mp4.")
    replay.add_argument("--chat", default=None, help="JSON con mensajes (t/ts/offset y text/message).")
    replay.add_argument("--cut", action="store_true", help="Escribe los momentos con ffmpeg -c copy.")
    replay.add_argument("--config", default="config.yaml")

    calibrate = sub.add_parser("calibrate", help="Sugiere umbrales y pesos a partir de las decisiones.")
    calibrate.add_argument("--config", default="config.yaml")
    calibrate.add_argument("--apply", action="store_true", help="Guarda la sugerencia en calibration.json.")
    calibrate.add_argument("--canal", default=None, help="Limita la sugerencia a un canal.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version or args.command is None and getattr(args, "version", False):
        print(__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 2
    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "clip":
            return asyncio.run(_cmd_clip(args))
        if args.command == "init-db":
            return asyncio.run(_cmd_init_db(args))
        if args.command == "telegram-setup":
            return asyncio.run(_cmd_telegram_setup(args))
        if args.command == "doctor":
            return doctor()
        if args.command == "replay":
            return replay_command(args)
        if args.command == "calibrate":
            return calibrate_command(Path(args.config), apply=bool(args.apply), canal=args.canal)
    except (AuthorizationRequired, ConfigError, ClipError) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if not isinstance(exc, ClipError) else 1
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrumpido.", file=sys.stderr)
        return 130
    return 2


async def _queue_review(config, env, repo) -> None:
    from cliphunter.review.service import build_review_runtime

    review = build_review_runtime(repo=repo, config=config, env=env, enable_local=False)
    try:
        await review.scan_once()
    except Exception as exc:
        log_event("WARNING", "review.submit_failed", error=str(exc))


async def _cmd_telegram_setup(args: argparse.Namespace) -> int:
    from cliphunter.review.state import state_path
    from cliphunter.review.telegram import HttpxTelegram, wait_for_start

    config = load_config(Path(args.config))
    env = load_env(Path(args.env))
    if not env.telegram_bot_token:
        print("Falta TELEGRAM_BOT_TOKEN en .env.", file=sys.stderr)
        return 2
    print("Abre Telegram, busca @clipowii_bot y envía /start. Esperando…")
    chat_id = await wait_for_start(HttpxTelegram(env.telegram_bot_token), state_path(config))
    print(f"Chat autorizado: {chat_id}")
    print(f"Guardado en {state_path(config)}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    env = load_env(Path(args.env))
    plan = prepare_run(config, skip_flag=args.yes)
    return asyncio.run(run_app(config, env, plan, hotkey_enabled=not args.no_hotkey))


async def _cmd_clip(args: argparse.Namespace) -> int:
    if args.ultimos <= 0:
        raise ClipError("--ultimos tiene que ser mayor que 0.")
    config = load_config(Path(args.config))
    env = load_env()
    channel = find_channel(config, args.canal)
    key = channel_key(channel)
    output = Path(args.output) if args.output else default_clip_path(config.resolved_clips_dir(), key)
    repo = SqliteRepo(config.resolved_database_path())
    await repo.init()
    try:
        channel_id = await repo.upsert_channel(
            channel_key=key,
            platform=channel.platform,
            handle=channel.handle,
            url=channel.url,
            authorized=channel.authorized,
            context=channel.context,
            weights=channel.weights,
            thresholds=channel.thresholds,
        )
        result = await create_clip(
            index_path=index_path_for(config, channel),
            seconds=float(args.ultimos),
            output=output,
            repo=repo,
            channel_id=channel_id,
            authorization=channel.authorization_payload(),
            credit=channel.credit,
            status="pending",
        )
        await _queue_review(config, env, repo)
    finally:
        await repo.close()
    print(f"Clip guardado en {result.path}")
    print(f"Metadatos en {result.sidecar}")
    if result.attribution.get("short_buffer"):
        print(
            "Aviso: el buffer tiene menos segundos de los pedidos; el clip es más corto.",
            file=sys.stderr,
        )
    log_event(
        "INFO",
        "clip.saved",
        path=str(result.path),
        actual_seconds=result.attribution.get("actual_seconds"),
    )
    return 0


async def _cmd_init_db(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    repo = SqliteRepo(config.resolved_database_path())
    await repo.init()
    await repo.close()
    print(f"Base lista en {config.resolved_database_path()}")
    return 0


def doctor() -> int:
    print(f"python {sys.version.split()[0]}")
    ok = True
    if sys.version_info < (3, 12):
        print("AVISO: ClipHunter está pensado para Python 3.12 o más nuevo.")
        ok = False
    checks = (
        ("ffmpeg", ["-version"], True),
        ("ffprobe", ["-version"], False),
        ("streamlink", ["--version"], True),
    )
    for tool, tool_args, required in checks:
        found = shutil.which(tool)
        if not found:
            print(f"{tool}: NO ENCONTRADO")
            if required:
                ok = False
            else:
                print("  Suele venir con ffmpeg. Sin él no se mide la duración del clip.")
            continue
        try:
            proc = subprocess.run(
                [found, *tool_args],
                capture_output=True,
                text=True,
                timeout=15,
            )
            line = (proc.stdout or proc.stderr or "").splitlines()
            detail = line[0] if line else "ok"
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = str(exc)
            if required:
                ok = False
        print(f"{tool}: {found}")
        print(f"  {detail}")
    if ok:
        print("Listo para la fase 1.")
    else:
        print("Falta alguna herramienta. Instálala y abre una terminal nueva para refrescar PATH.")
    return 0 if ok else 1
