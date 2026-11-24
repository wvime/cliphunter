"""Storage interface. SQLite today, Postgres later behind the same Repo."""

from cliphunter.storage.repo import Repo, SqliteRepo

__all__ = ["Repo", "SqliteRepo"]
