import aiosqlite
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    discord_name TEXT NOT NULL,
    character_name TEXT NOT NULL,
    is_dm INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    summary_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    voice_channel_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active | paused | ended
    transcript_path TEXT,
    summary_text TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # Safe migration for databases created before is_dm existed.
        try:
            await db.execute("ALTER TABLE characters ADD COLUMN is_dm INTEGER NOT NULL DEFAULT 0")
            await db.commit()
        except aiosqlite.OperationalError:
            pass  # column already exists
        await db.commit()


async def link_character(guild_id: int, user_id: int, discord_name: str, character_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO characters (guild_id, user_id, discord_name, character_name)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET
                 discord_name=excluded.discord_name,
                 character_name=excluded.character_name""",
            (guild_id, user_id, discord_name, character_name),
        )
        await db.commit()


async def set_dm(guild_id: int, user_id: int, discord_name: str, title: str = "Dungeon Master"):
    """Marks a user as the DM, clearing DM status from anyone else in this guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE characters SET is_dm = 0 WHERE guild_id = ?", (guild_id,)
        )
        await db.execute(
            """INSERT INTO characters (guild_id, user_id, discord_name, character_name, is_dm)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET
                 discord_name=excluded.discord_name,
                 character_name=excluded.character_name,
                 is_dm=1""",
            (guild_id, user_id, discord_name, title),
        )
        await db.commit()


async def get_characters(guild_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM characters WHERE guild_id = ?", (guild_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_character_name(guild_id: int, user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT character_name FROM characters WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_character_info(guild_id: int, user_id: int) -> dict | None:
    """Returns {'character_name': str, 'is_dm': bool} or None if not linked."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT character_name, is_dm FROM characters WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {"character_name": row[0], "is_dm": bool(row[1])}


async def set_summary_channel(guild_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO guild_config (guild_id, summary_channel_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET summary_channel_id=excluded.summary_channel_id""",
            (guild_id, channel_id),
        )
        await db.commit()


async def get_summary_channel(guild_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT summary_channel_id FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def create_session(guild_id: int, voice_channel_id: int, started_at: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO sessions (guild_id, voice_channel_id, started_at) VALUES (?, ?, ?)",
            (guild_id, voice_channel_id, started_at),
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_session(guild_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND status IN ('active','paused') "
            "ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_session_status(session_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))
        await db.commit()


async def finish_session(session_id: int, ended_at: str, transcript_path: str, summary_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE sessions SET status='ended', ended_at=?, transcript_path=?, summary_text=?
               WHERE id = ?""",
            (ended_at, transcript_path, summary_text, session_id),
        )
        await db.commit()


async def get_recent_sessions(guild_id: int, limit: int = 5) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND status='ended' "
            "ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def search_sessions(guild_id: int, term: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND status='ended' "
            "AND summary_text LIKE ? ORDER BY id DESC",
            (guild_id, f"%{term}%"),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
