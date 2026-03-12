import aiosqlite
import os

DB_PATH = os.environ.get("DB_PATH", "tracker.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                place_id TEXT NOT NULL,
                place_name TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rankings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                place_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                rank INTEGER,
                checked_at TEXT DEFAULT (datetime('now','localtime')),
                date TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_rankings_date 
            ON rankings(place_id, keyword, date)
        """)
        await db.commit()

async def get_places():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM places ORDER BY id DESC") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def add_place(place_id: str, place_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO places (place_id, place_name) VALUES (?, ?)",
            (place_id, place_name)
        )
        await db.commit()

async def delete_place(place_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM places WHERE place_id = ?", (place_id,))
        await db.execute("DELETE FROM rankings WHERE place_id = ?", (place_id,))
        await db.commit()

async def get_keywords():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM keywords ORDER BY id DESC") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def add_keyword(keyword: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (keyword,)
        )
        await db.commit()

async def delete_keyword(keyword: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM keywords WHERE keyword = ?", (keyword,))
        await db.execute("DELETE FROM rankings WHERE keyword = ?", (keyword,))
        await db.commit()

async def save_ranking(place_id: str, keyword: str, rank: int, date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # 같은 날 같은 데이터 있으면 업데이트
        await db.execute("""
            INSERT INTO rankings (place_id, keyword, rank, date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """, (place_id, keyword, rank, date))
        # 이미 있으면 업데이트
        await db.execute("""
            UPDATE rankings SET rank = ?, checked_at = datetime('now','localtime')
            WHERE place_id = ? AND keyword = ? AND date = ?
        """, (rank, place_id, keyword, date))
        await db.commit()

async def get_rankings(place_id: str = None, keyword: str = None, days: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT r.*, p.place_name 
            FROM rankings r
            LEFT JOIN places p ON r.place_id = p.place_id
            WHERE r.date >= date('now', ?, 'localtime')
        """
        params = [f"-{days} days"]
        if place_id:
            query += " AND r.place_id = ?"
            params.append(place_id)
        if keyword:
            query += " AND r.keyword = ?"
            params.append(keyword)
        query += " ORDER BY r.date DESC, r.checked_at DESC"
        async with db.execute(query, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_latest_rankings():
    """각 place_id + keyword 조합의 가장 최근 순위"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT r.place_id, r.keyword, r.rank, r.date, r.checked_at, p.place_name
            FROM rankings r
            LEFT JOIN places p ON r.place_id = p.place_id
            WHERE r.id IN (
                SELECT MAX(id) FROM rankings GROUP BY place_id, keyword
            )
            ORDER BY r.keyword, r.rank
        """) as cursor:
            return [dict(row) for row in await cursor.fetchall()]
