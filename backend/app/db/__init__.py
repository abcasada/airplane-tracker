import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

Params = Sequence[Any] | dict[str, Any]


def _migrations() -> list[tuple[int, str, str]]:
    files = resources.files("app.db") / "migrations"
    out = []
    for f in files.iterdir():
        if f.name.endswith(".sql"):
            out.append((int(f.name.split("_", 1)[0]), f.name, f.read_text()))
    return sorted(out)


def connect_sync(path: Path) -> sqlite3.Connection:
    """A separate synchronous connection, used by bulk dataset imports in a worker thread."""
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database not connected")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, timeout=30)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def migrate(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT)"
        )
        async with self.conn.execute("SELECT version FROM schema_migrations") as cur:
            applied = {row[0] for row in await cur.fetchall()}
        for version, name, sql in _migrations():
            if version in applied:
                continue
            log.info("applying migration %s", name)
            record = f"INSERT INTO schema_migrations VALUES ({version}, '{name}');"  # noqa: S608
            await self.conn.executescript(f"BEGIN;\n{sql}\n{record}\nCOMMIT;")

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Run a statement and commit. Returns the number of affected rows."""
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.rowcount

    async def insert(self, sql: str, params: Params = ()) -> int:
        """Run an INSERT and commit. Returns the new rowid."""
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def executemany(self, sql: str, rows: Iterable[Params]) -> None:
        await self.conn.executemany(sql, rows)
        await self.conn.commit()

    async def fetchall(self, sql: str, params: Params = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Params = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def get_json(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row[0]) if row else default

    async def set_json(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    async def audit(self, at: float, actor: str | None, action: str, detail: Any = None) -> None:
        await self.execute(
            "INSERT INTO audit_log(at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (at, actor, action, json.dumps(detail) if detail is not None else None),
        )
