"""Import participants and publishing metrics."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def _verified(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "si", "sí", "yes", "verificado"}
    return False


async def import_participants(path: Path, repo) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    people = payload
    relationships = []
    storylines = []
    if isinstance(payload, dict):
        people = payload.get("participantes") or payload.get("participants") or []
        relationships = payload.get("relaciones") or payload.get("relationships") or []
        storylines = payload.get("storylines") or payload.get("tramas") or []
    if not isinstance(people, list):
        raise ValueError("el JSON de participantes tiene que ser una lista o un objeto con participantes")
    count = 0
    for item in people:
        if not isinstance(item, dict) or not item.get("nombre") and not item.get("name"):
            continue
        name = str(item.get("nombre") or item.get("name")).strip()
        nicknames = item.get("apodos") or item.get("nicknames") or []
        if isinstance(nicknames, str):
            nicknames = [part.strip() for part in nicknames.split(",") if part.strip()]
        await repo.upsert_participant(
            name=name,
            description=item.get("descripcion") or item.get("description"),
            persona=item.get("persona"),
            source=item.get("fuente") or item.get("source"),
            verified=_verified(item.get("verificado", item.get("verified", False))),
            nicknames=[str(nick) for nick in nicknames],
        )
        count += 1
        for link in item.get("relaciones") or []:
            if isinstance(link, dict):
                relationships.append({**link, "left": name})
    for link in relationships:
        if not isinstance(link, dict):
            continue
        right = link.get("con") or link.get("right") or link.get("nombre")
        kind = link.get("tipo") or link.get("kind")
        left = str(link.get("left") or link.get("desde") or "").strip()
        if not right or not kind or not left:
            continue
        await repo.add_relationship(
            left_name=left,
            right_name=str(right),
            kind=str(kind),
            detail=link.get("detalle") or link.get("detail"),
            source=link.get("fuente") or link.get("source"),
            verified=_verified(link.get("verificado", link.get("verified", False))),
        )
    for story in storylines:
        if not isinstance(story, dict) or not (story.get("titulo") or story.get("title")):
            continue
        names = story.get("participantes") or story.get("participants") or []
        await repo.add_storyline(
            title=str(story.get("titulo") or story.get("title")),
            detail=story.get("detalle") or story.get("detail"),
            participants=[str(name) for name in names],
            source=story.get("fuente") or story.get("source"),
            verified=_verified(story.get("verificado", story.get("verified", False))),
        )
    return count


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return float(text.replace(",", "."))


async def import_metrics(path: Path, repo) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    count = 0
    for row in rows:
        lowered = { (key or "").strip().casefold(): value for key, value in row.items() }
        clip_raw = lowered.get("clip_id") or lowered.get("clip")
        clip_id = int(clip_raw) if clip_raw and clip_raw.strip() else None
        await repo.add_publish_result(
            clip_id=clip_id,
            platform=lowered.get("platform") or lowered.get("plataforma"),
            retention=_num(lowered.get("retention") or lowered.get("retencion")),
            shares=_num(lowered.get("shares") or lowered.get("compartidos")),
            comments=_num(lowered.get("comments") or lowered.get("comentarios")),
            replay_rate=_num(lowered.get("replay_rate") or lowered.get("replay")),
            source=str(path),
            raw=lowered,
        )
        count += 1
    return count
