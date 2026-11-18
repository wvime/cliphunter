"""SQLite repository. One connection per process, WAL so ``clip`` can write while ``run`` is open."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

import aiosqlite

from cliphunter.timeutil import utc_iso

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


_COLUMN_PATCHES = {
    ("clips", "weight"): "REAL",
    ("clips", "review_claimed"): "INTEGER NOT NULL DEFAULT 0",
    ("decisions", "weight"): "REAL",
    ("decisions", "breakdown_json"): "TEXT",
}


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, ddl: str) -> None:
    """Add a column when an older database file is opened again."""

    expected = _COLUMN_PATCHES.get((table, column))
    if expected != ddl:
        raise ValueError(f"unsupported schema patch {table}.{column}")
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    names = {row["name"] for row in rows}
    if column not in names:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


class Repo(Protocol):
    async def init(self) -> None: ...

    async def close(self) -> None: ...

    async def upsert_channel(
        self,
        *,
        channel_key: str,
        platform: str,
        handle: str | None,
        url: str | None,
        authorized: bool,
        context: str,
        weights: dict[str, float],
        thresholds: dict[str, float],
    ) -> int: ...

    async def get_channel(self, channel_id: int) -> dict[str, Any] | None: ...

    async def get_channel_by_key(self, channel_key: str) -> dict[str, Any] | None: ...

    async def start_session(
        self,
        *,
        channel_id: int,
        stream_url: str | None,
        stream_title: str | None,
        video_id: str | None,
        confirmed_authorized_at: str | None,
    ) -> int: ...

    async def end_session(self, session_id: int, *, status: str = "ended") -> None: ...

    async def interrupt_open_sessions(self) -> int: ...

    async def session_exists(self, session_id: int) -> bool: ...

    async def get_session(self, session_id: int) -> dict[str, Any] | None: ...

    async def insert_clip(
        self,
        *,
        channel_id: int,
        kind: str,
        status: str,
        attribution: dict[str, Any],
        file_path: str | None,
        session_id: int | None = None,
        signal_score: float | None = None,
        breakdown: dict[str, Any] | None = None,
        title: str | None = None,
        hook: str | None = None,
        hashtags: list[str] | None = None,
        duration_seconds: float | None = None,
        weight: float | None = None,
    ) -> int: ...

    async def insert_decision(
        self,
        *,
        clip_id: int,
        action: str,
        adjust_seconds: float | None = None,
        note: str | None = None,
        weight: float | None = None,
        breakdown: dict[str, Any] | None = None,
    ) -> int: ...

    async def get_clip(self, clip_id: int) -> dict[str, Any] | None: ...

    async def update_clip(
        self,
        clip_id: int,
        *,
        status: str | None = None,
        attribution: dict[str, Any] | None = None,
        file_path: str | None = None,
        duration_seconds: float | None = None,
    ) -> None: ...

    async def claim_review(self, clip_id: int) -> bool: ...

    async def release_review(self, clip_id: int) -> None: ...

    async def list_decisions(self, clip_id: int | None = None) -> list[dict[str, Any]]: ...

    async def insert_metric(
        self,
        *,
        session_id: int | None,
        cpu_pct: float | None,
        rss_mb: float | None,
        extra: dict[str, Any] | None = None,
    ) -> int: ...

    async def list_clips(self, channel_id: int | None = None) -> list[dict[str, Any]]: ...

    async def list_metrics(self, session_id: int | None = None) -> list[dict[str, Any]]: ...


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


class SqliteRepo:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> aiosqlite.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self.path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._db.execute("PRAGMA busy_timeout=5000")
        return self._db

    async def init(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        async with self._lock:
            db = await self._connect()
            await db.executescript(schema)
            for (table, column), ddl in _COLUMN_PATCHES.items():
                await _ensure_column(db, table, column, ddl)
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    async def __aenter__(self) -> SqliteRepo:
        await self.init()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def upsert_channel(
        self,
        *,
        channel_key: str,
        platform: str,
        handle: str | None,
        url: str | None,
        authorized: bool,
        context: str,
        weights: dict[str, float],
        thresholds: dict[str, float],
    ) -> int:
        now = utc_iso()
        async with self._lock:
            db = await self._connect()
            await db.execute(
                """
                INSERT INTO channels (
                    channel_key, platform, handle, url, authorized, context,
                    weights_json, thresholds_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_key) DO UPDATE SET
                    platform=excluded.platform,
                    handle=excluded.handle,
                    url=excluded.url,
                    authorized=excluded.authorized,
                    context=excluded.context,
                    weights_json=excluded.weights_json,
                    thresholds_json=excluded.thresholds_json,
                    updated_at=excluded.updated_at
                """,
                (
                    channel_key,
                    platform,
                    handle,
                    url,
                    1 if authorized else 0,
                    context,
                    _dumps(weights),
                    _dumps(thresholds),
                    now,
                    now,
                ),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT id FROM channels WHERE channel_key = ?",
                (channel_key,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError(f"channel {channel_key} disappeared after upsert")
            return int(row["id"])

    async def get_channel(self, channel_id: int) -> dict[str, Any] | None:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_channel_by_key(self, channel_key: str) -> dict[str, Any] | None:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute(
                "SELECT * FROM channels WHERE channel_key = ?",
                (channel_key,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def start_session(
        self,
        *,
        channel_id: int,
        stream_url: str | None,
        stream_title: str | None,
        video_id: str | None,
        confirmed_authorized_at: str | None,
    ) -> int:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute(
                """
                INSERT INTO sessions (
                    channel_id, stream_url, stream_title, video_id,
                    started_at, confirmed_authorized_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    channel_id,
                    stream_url,
                    stream_title,
                    video_id,
                    utc_iso(),
                    confirmed_authorized_at,
                ),
            )
            await db.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("session insert did not return an id")
            return int(cursor.lastrowid)

    async def end_session(self, session_id: int, *, status: str = "ended") -> None:
        async with self._lock:
            db = await self._connect()
            await db.execute(
                """
                UPDATE sessions
                SET status = ?, ended_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (status, utc_iso(), session_id),
            )
            await db.commit()

    async def interrupt_open_sessions(self) -> int:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute(
                """
                UPDATE sessions
                SET status = 'interrupted', ended_at = ?
                WHERE status = 'active'
                """,
                (utc_iso(),),
            )
            await db.commit()
            return cursor.rowcount or 0

    async def session_exists(self, session_id: int) -> bool:
        return await self.get_session(session_id) is not None

    async def get_session(self, session_id: int) -> dict[str, Any] | None:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def insert_clip(
        self,
        *,
        channel_id: int,
        kind: str,
        status: str,
        attribution: dict[str, Any],
        file_path: str | None,
        session_id: int | None = None,
        signal_score: float | None = None,
        breakdown: dict[str, Any] | None = None,
        title: str | None = None,
        hook: str | None = None,
        hashtags: list[str] | None = None,
        duration_seconds: float | None = None,
        weight: float | None = None,
    ) -> int:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute(
                """
                INSERT INTO clips (
                    session_id, channel_id, kind, status, signal_score,
                    breakdown_json, title, hook, hashtags, attribution_json,
                    file_path, duration_seconds, weight, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    channel_id,
                    kind,
                    status,
                    signal_score,
                    _dumps(breakdown) if breakdown is not None else None,
                    title,
                    hook,
                    _dumps(hashtags) if hashtags is not None else None,
                    _dumps(attribution),
                    file_path,
                    duration_seconds,
                    weight,
                    utc_iso(),
                ),
            )
            await db.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("clip insert did not return an id")
            return int(cursor.lastrowid)

    async def insert_decision(
        self,
        *,
        clip_id: int,
        action: str,
        adjust_seconds: float | None = None,
        note: str | None = None,
        weight: float | None = None,
        breakdown: dict[str, Any] | None = None,
    ) -> int:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute(
                """
                INSERT INTO decisions (
                    clip_id, action, adjust_seconds, note, weight, breakdown_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clip_id,
                    action,
                    adjust_seconds,
                    note,
                    weight,
                    _dumps(breakdown) if breakdown is not None else None,
                    utc_iso(),
                ),
            )
            await db.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("decision insert did not return an id")
            return int(cursor.lastrowid)

    async def insert_metric(
        self,
        *,
        session_id: int | None,
        cpu_pct: float | None,
        rss_mb: float | None,
        extra: dict[str, Any] | None = None,
    ) -> int:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute(
                """
                INSERT INTO metrics (session_id, cpu_pct, rss_mb, extra_json, sampled_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    cpu_pct,
                    rss_mb,
                    _dumps(extra) if extra is not None else None,
                    utc_iso(),
                ),
            )
            await db.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("metric insert did not return an id")
            return int(cursor.lastrowid)

    async def list_clips(self, channel_id: int | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            db = await self._connect()
            if channel_id is None:
                cursor = await db.execute("SELECT * FROM clips ORDER BY id")
            else:
                cursor = await db.execute(
                    "SELECT * FROM clips WHERE channel_id = ? ORDER BY id",
                    (channel_id,),
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def list_metrics(self, session_id: int | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            db = await self._connect()
            if session_id is None:
                cursor = await db.execute("SELECT * FROM metrics ORDER BY id")
            else:
                cursor = await db.execute(
                    "SELECT * FROM metrics WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_clip(self, clip_id: int) -> dict[str, Any] | None:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute("SELECT * FROM clips WHERE id = ?", (clip_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_clip(
        self,
        clip_id: int,
        *,
        status: str | None = None,
        attribution: dict[str, Any] | None = None,
        file_path: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if attribution is not None:
            sets.append("attribution_json = ?")
            params.append(_dumps(attribution))
        if file_path is not None:
            sets.append("file_path = ?")
            params.append(file_path)
        if duration_seconds is not None:
            sets.append("duration_seconds = ?")
            params.append(duration_seconds)
        if not sets:
            return
        params.append(clip_id)
        async with self._lock:
            db = await self._connect()
            await db.execute(f"UPDATE clips SET {', '.join(sets)} WHERE id = ?", params)
            await db.commit()

    async def claim_review(self, clip_id: int) -> bool:
        async with self._lock:
            db = await self._connect()
            cursor = await db.execute(
                """
                UPDATE clips
                SET review_claimed = 1
                WHERE id = ? AND review_claimed = 0 AND status = 'pending'
                """,
                (clip_id,),
            )
            await db.commit()
            return (cursor.rowcount or 0) == 1

    async def release_review(self, clip_id: int) -> None:
        async with self._lock:
            db = await self._connect()
            await db.execute("UPDATE clips SET review_claimed = 0 WHERE id = ?", (clip_id,))
            await db.commit()

    async def list_decisions(self, clip_id: int | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            db = await self._connect()
            if clip_id is None:
                cursor = await db.execute("SELECT * FROM decisions ORDER BY id")
            else:
                cursor = await db.execute(
                    "SELECT * FROM decisions WHERE clip_id = ? ORDER BY id",
                    (clip_id,),
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
