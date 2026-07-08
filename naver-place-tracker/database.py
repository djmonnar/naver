import asyncio
import base64
import os
from datetime import datetime, timedelta

import aiosqlite

from firebase_config import get_admin_app, uses_firestore


DB_PATH = os.environ.get("DB_PATH", "tracker.db")
DEFAULT_USER_ID = os.environ.get("DEFAULT_USER_ID", "default")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None


def _user_id(user_id: str | None) -> str:
    return user_id or DEFAULT_USER_ID


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _keyword_doc_id(keyword: str) -> str:
    encoded = base64.urlsafe_b64encode(keyword.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def _place_keyword_doc_id(place_id: str, keyword: str) -> str:
    place_key = base64.urlsafe_b64encode(str(place_id).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{place_key}_{_keyword_doc_id(keyword)}"


async def init_db():
    if uses_firestore():
        # Defer Firebase Admin initialization until an authenticated request or
        # crawler run. This keeps /ping and diagnostics alive even when a Render
        # environment variable is malformed.
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_uid TEXT NOT NULL DEFAULT 'default',
                place_id TEXT NOT NULL,
                place_name TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_uid TEXT NOT NULL DEFAULT 'default',
                place_id TEXT NOT NULL DEFAULT '',
                keyword TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(owner_uid, place_id, keyword)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rankings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_uid TEXT NOT NULL DEFAULT 'default',
                place_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                rank INTEGER,
                checked_at TEXT DEFAULT (datetime('now','localtime')),
                date TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS keyword_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_uid TEXT NOT NULL DEFAULT 'default',
                keyword TEXT NOT NULL,
                rank INTEGER NOT NULL,
                place_id TEXT NOT NULL DEFAULT '',
                place_name TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL,
                checked_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)

        await _ensure_column(db, "places", "owner_uid", "TEXT NOT NULL DEFAULT 'default'")
        await _ensure_column(db, "keywords", "owner_uid", "TEXT NOT NULL DEFAULT 'default'")
        await _ensure_column(db, "keywords", "place_id", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "rankings", "owner_uid", "TEXT NOT NULL DEFAULT 'default'")
        await _ensure_column(db, "keyword_results", "owner_uid", "TEXT NOT NULL DEFAULT 'default'")
        await _rebuild_keywords_if_global_unique(db)
        await _rebuild_keywords_if_missing_place_unique(db)

        await db.execute("""
            DELETE FROM places
            WHERE id NOT IN (
                SELECT MAX(id) FROM places GROUP BY owner_uid, place_id
            )
        """)
        await db.execute("""
            DELETE FROM keywords
            WHERE id NOT IN (
                SELECT MAX(id) FROM keywords GROUP BY owner_uid, place_id, keyword
            )
        """)
        await db.execute("""
            DELETE FROM rankings
            WHERE id NOT IN (
                SELECT MAX(id) FROM rankings GROUP BY owner_uid, place_id, keyword, date
            )
        """)
        await db.execute("""
            DELETE FROM keyword_results
            WHERE id NOT IN (
                SELECT MAX(id) FROM keyword_results GROUP BY owner_uid, keyword, date, rank
            )
        """)

        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_places_owner_place
            ON places(owner_uid, place_id)
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_keywords_owner_place_keyword
            ON keywords(owner_uid, place_id, keyword)
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rankings_owner_date
            ON rankings(owner_uid, place_id, keyword, date)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_rankings_date
            ON rankings(owner_uid, place_id, keyword, date)
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_keyword_results_rank
            ON keyword_results(owner_uid, keyword, date, rank)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_keyword_results_keyword_date
            ON keyword_results(owner_uid, keyword, date)
        """)
        await db.commit()


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        columns = [row[1] for row in await cursor.fetchall()]
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def _rebuild_keywords_if_global_unique(db: aiosqlite.Connection) -> None:
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='keywords'"
    ) as cursor:
        row = await cursor.fetchone()
    table_sql = (row[0] or "").lower() if row else ""
    has_owner_unique = "unique(owner_uid, keyword)" in table_sql or "unique (owner_uid, keyword)" in table_sql
    has_global_unique = "keyword text not null unique" in table_sql or "unique(keyword)" in table_sql

    if not has_global_unique or has_owner_unique:
        return

    await db.execute("ALTER TABLE keywords RENAME TO keywords_old")
    await db.execute("""
        CREATE TABLE keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_uid TEXT NOT NULL DEFAULT 'default',
            keyword TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(owner_uid, keyword)
        )
    """)
    await db.execute("""
        INSERT OR IGNORE INTO keywords (id, owner_uid, keyword, created_at)
        SELECT id, COALESCE(owner_uid, 'default'), keyword, created_at
        FROM keywords_old
    """)
    await db.execute("DROP TABLE keywords_old")


async def _rebuild_keywords_if_missing_place_unique(db: aiosqlite.Connection) -> None:
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='keywords'"
    ) as cursor:
        row = await cursor.fetchone()
    table_sql = (row[0] or "").lower() if row else ""
    has_place_unique = (
        "unique(owner_uid, place_id, keyword)" in table_sql
        or "unique (owner_uid, place_id, keyword)" in table_sql
    )

    if has_place_unique:
        return

    await db.execute("ALTER TABLE keywords RENAME TO keywords_old")
    await db.execute("""
        CREATE TABLE keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_uid TEXT NOT NULL DEFAULT 'default',
            place_id TEXT NOT NULL DEFAULT '',
            keyword TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(owner_uid, place_id, keyword)
        )
    """)
    await db.execute("""
        INSERT OR IGNORE INTO keywords (id, owner_uid, place_id, keyword, created_at)
        SELECT id, COALESCE(owner_uid, 'default'), COALESCE(place_id, ''), keyword, created_at
        FROM keywords_old
    """)
    await db.execute("DROP TABLE keywords_old")


def _firestore():
    from firebase_admin import firestore

    get_admin_app()
    return firestore.client()


def _user_doc(user_id: str):
    return _firestore().collection("users").document(_user_id(user_id))


def _ensure_firestore_user_sync(
    user_id: str,
    email: str | None = None,
    name: str | None = None,
    photo_url: str | None = None,
) -> None:
    data = {"updated_at": _now_text()}
    if email:
        data["email"] = email
    if name:
        data["name"] = name
    if photo_url:
        data["photo_url"] = photo_url
    _user_doc(user_id).set(data, merge=True)


async def ensure_user(
    user_id: str | None,
    email: str | None = None,
    name: str | None = None,
    photo_url: str | None = None,
) -> None:
    owner_uid = _user_id(user_id)
    if uses_firestore():
        await asyncio.to_thread(_ensure_firestore_user_sync, owner_uid, email, name, photo_url)


async def set_check_status(
    user_id: str | None,
    state: str,
    message: str,
    details: dict | None = None,
) -> None:
    owner_uid = _user_id(user_id)
    payload = {
        "state": state,
        "message": message,
        "updated_at": _now_text(),
    }
    if details:
        payload.update(details)

    if uses_firestore():
        def _set_sync():
            _ensure_firestore_user_sync(owner_uid)
            _user_doc(owner_uid).set({"check_status": payload}, merge=True)

        await asyncio.to_thread(_set_sync)
        return


async def consume_manual_check_quota(user_id: str | None, date: str, limit: int) -> dict:
    owner_uid = _user_id(user_id)
    safe_limit = max(0, int(limit or 0))
    if safe_limit <= 0:
        return {"allowed": True, "count": 0, "remaining": None, "date": date, "limit": safe_limit}

    if uses_firestore():
        def _consume_sync():
            from firebase_admin import firestore

            db = _firestore()
            ref = _user_doc(owner_uid)
            transaction = db.transaction()

            @firestore.transactional
            def _consume(transaction):
                snapshot = ref.get(transaction=transaction)
                data = snapshot.to_dict() or {}
                usage = data.get("manual_check_usage") or {}
                current_count = int(usage.get("count") or 0) if usage.get("date") == date else 0
                if current_count >= safe_limit:
                    return {
                        "allowed": False,
                        "count": current_count,
                        "remaining": 0,
                        "date": date,
                        "limit": safe_limit,
                    }

                next_count = current_count + 1
                transaction.set(ref, {
                    "manual_check_usage": {
                        "date": date,
                        "count": next_count,
                        "limit": safe_limit,
                        "updated_at": _now_text(),
                    }
                }, merge=True)
                return {
                    "allowed": True,
                    "count": next_count,
                    "remaining": max(safe_limit - next_count, 0),
                    "date": date,
                    "limit": safe_limit,
                }

            return _consume(transaction)

        return await asyncio.to_thread(_consume_sync)

    return {"allowed": True, "count": 0, "remaining": None, "date": date, "limit": safe_limit}


async def list_user_ids() -> list[str]:
    if uses_firestore():
        def _list_sync():
            return [doc.id for doc in _firestore().collection("users").stream()]

        return await asyncio.to_thread(_list_sync)

    async with aiosqlite.connect(DB_PATH) as db:
        rows: list[str] = []
        for table in ("places", "keywords", "rankings", "keyword_results"):
            async with db.execute(f"SELECT DISTINCT owner_uid FROM {table}") as cursor:
                rows.extend([row[0] for row in await cursor.fetchall() if row[0]])
        return sorted(set(rows))


async def get_places(user_id: str | None = None):
    owner_uid = _user_id(user_id)
    if uses_firestore():
        def _get_sync():
            docs = _user_doc(owner_uid).collection("places").stream()
            rows = [doc.to_dict() for doc in docs]
            return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

        return await asyncio.to_thread(_get_sync)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM places WHERE owner_uid = ? ORDER BY id DESC",
            (owner_uid,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def add_place(place_id: str, place_name: str, user_id: str | None = None):
    owner_uid = _user_id(user_id)
    if uses_firestore():
        def _add_sync():
            _ensure_firestore_user_sync(owner_uid)
            _user_doc(owner_uid).collection("places").document(place_id).set({
                "place_id": place_id,
                "place_name": place_name,
                "created_at": _now_text(),
            }, merge=True)

        await asyncio.to_thread(_add_sync)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO places (owner_uid, place_id, place_name)
            VALUES (?, ?, ?)
            ON CONFLICT(owner_uid, place_id)
            DO UPDATE SET place_name = excluded.place_name
            """,
            (owner_uid, place_id, place_name),
        )
        await db.commit()


async def delete_place(place_id: str, user_id: str | None = None):
    owner_uid = _user_id(user_id)
    if uses_firestore():
        def _delete_sync():
            batch = _firestore().batch()
            batch.delete(_user_doc(owner_uid).collection("places").document(place_id))
            rankings = (
                _user_doc(owner_uid)
                .collection("rankings")
                .where("place_id", "==", place_id)
                .stream()
            )
            for doc in rankings:
                batch.delete(doc.reference)
            keywords = (
                _user_doc(owner_uid)
                .collection("keywords")
                .where("place_id", "==", place_id)
                .stream()
            )
            for doc in keywords:
                batch.delete(doc.reference)
            batch.commit()

        await asyncio.to_thread(_delete_sync)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM places WHERE owner_uid = ? AND place_id = ?",
            (owner_uid, place_id),
        )
        await db.execute(
            "DELETE FROM rankings WHERE owner_uid = ? AND place_id = ?",
            (owner_uid, place_id),
        )
        await db.execute(
            "DELETE FROM keywords WHERE owner_uid = ? AND place_id = ?",
            (owner_uid, place_id),
        )
        await db.commit()


async def get_keywords(user_id: str | None = None):
    owner_uid = _user_id(user_id)
    if uses_firestore():
        def _get_sync():
            docs = _user_doc(owner_uid).collection("keywords").stream()
            rows = [{"id": doc.id, **(doc.to_dict() or {})} for doc in docs]
            return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

        return await asyncio.to_thread(_get_sync)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM keywords WHERE owner_uid = ? ORDER BY id DESC",
            (owner_uid,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def add_keyword(keyword: str, user_id: str | None = None, place_id: str | None = None):
    owner_uid = _user_id(user_id)
    place_key = str(place_id or "")
    if uses_firestore():
        def _add_sync():
            _ensure_firestore_user_sync(owner_uid)
            doc_id = _place_keyword_doc_id(place_key, keyword) if place_key else _keyword_doc_id(keyword)
            _user_doc(owner_uid).collection("keywords").document(doc_id).set({
                "keyword": keyword,
                "place_id": place_key,
                "created_at": _now_text(),
            }, merge=True)

        await asyncio.to_thread(_add_sync)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO keywords (owner_uid, place_id, keyword) VALUES (?, ?, ?)",
            (owner_uid, place_key, keyword),
        )
        await db.commit()


async def delete_keyword(keyword: str, user_id: str | None = None, place_id: str | None = None):
    owner_uid = _user_id(user_id)
    place_key = str(place_id or "")
    if uses_firestore():
        def _delete_sync():
            batch = _firestore().batch()
            collection = _user_doc(owner_uid).collection("keywords")
            doc_id = _place_keyword_doc_id(place_key, keyword) if place_key else _keyword_doc_id(keyword)
            batch.delete(collection.document(doc_id))
            if place_key:
                batch.delete(collection.document(_keyword_doc_id(keyword)))
            rankings_query = (
                _user_doc(owner_uid)
                .collection("rankings")
                .where("keyword", "==", keyword)
            )
            if place_key:
                rankings_query = rankings_query.where("place_id", "==", place_key)
            rankings = rankings_query.stream()
            for doc in rankings:
                batch.delete(doc.reference)
            batch.commit()

        await asyncio.to_thread(_delete_sync)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        if place_key:
            await db.execute(
                "DELETE FROM keywords WHERE owner_uid = ? AND place_id = ? AND keyword = ?",
                (owner_uid, place_key, keyword),
            )
            await db.execute(
                "DELETE FROM rankings WHERE owner_uid = ? AND place_id = ? AND keyword = ?",
                (owner_uid, place_key, keyword),
            )
        else:
            await db.execute(
                "DELETE FROM keywords WHERE owner_uid = ? AND keyword = ?",
                (owner_uid, keyword),
            )
            await db.execute(
                "DELETE FROM rankings WHERE owner_uid = ? AND keyword = ?",
                (owner_uid, keyword),
            )
        await db.commit()


async def save_ranking(place_id: str, keyword: str, rank: int, date: str, user_id: str | None = None):
    owner_uid = _user_id(user_id)
    if uses_firestore():
        def _save_sync():
            keyword_id = _keyword_doc_id(keyword)
            doc_id = f"{place_id}_{keyword_id}_{date}"
            _ensure_firestore_user_sync(owner_uid)
            _user_doc(owner_uid).collection("rankings").document(doc_id).set({
                "place_id": place_id,
                "keyword": keyword,
                "rank": rank,
                "date": date,
                "checked_at": _now_text(),
            }, merge=True)

        await asyncio.to_thread(_save_sync)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO rankings (owner_uid, place_id, keyword, rank, date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner_uid, place_id, keyword, date)
            DO UPDATE SET rank = excluded.rank, checked_at = datetime('now','localtime')
        """, (owner_uid, place_id, keyword, rank, date))
        await db.commit()


async def save_keyword_results(
    keyword: str,
    results: list[dict],
    date: str,
    user_id: str | None = None,
):
    owner_uid = _user_id(user_id)
    clean_results = [
        {
            "rank": int(row.get("rank") or 0),
            "place_id": str(row.get("place_id") or ""),
            "place_name": str(row.get("place_name") or ""),
        }
        for row in results
        if int(row.get("rank") or 0) > 0
    ]

    if uses_firestore():
        def _save_sync():
            keyword_id = _keyword_doc_id(keyword)
            collection = _user_doc(owner_uid).collection("keyword_results")
            batch = _firestore().batch()
            existing = (
                collection
                .where("keyword", "==", keyword)
                .where("date", "==", date)
                .stream()
            )
            for doc in existing:
                batch.delete(doc.reference)
            for row in clean_results:
                doc_id = f"{keyword_id}_{date}_{row['rank']:03d}"
                batch.set(collection.document(doc_id), {
                    "keyword": keyword,
                    "rank": row["rank"],
                    "place_id": row["place_id"],
                    "place_name": row["place_name"],
                    "date": date,
                    "checked_at": _now_text(),
                }, merge=True)
            _ensure_firestore_user_sync(owner_uid)
            batch.commit()

        await asyncio.to_thread(_save_sync)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM keyword_results WHERE owner_uid = ? AND keyword = ? AND date = ?",
            (owner_uid, keyword, date),
        )
        await db.executemany(
            """
            INSERT INTO keyword_results (owner_uid, keyword, rank, place_id, place_name, date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (owner_uid, keyword, row["rank"], row["place_id"], row["place_name"], date)
                for row in clean_results
            ],
        )
        await db.commit()


async def get_rankings(
    place_id: str | None = None,
    keyword: str | None = None,
    days: int = 30,
    user_id: str | None = None,
):
    owner_uid = _user_id(user_id)
    if uses_firestore():
        def _get_sync():
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            place_map = {
                row["place_id"]: row.get("place_name", "")
                for row in [_doc.to_dict() for _doc in _user_doc(owner_uid).collection("places").stream()]
            }
            rows = []
            for doc in _user_doc(owner_uid).collection("rankings").stream():
                row = doc.to_dict()
                if row.get("date", "") < cutoff:
                    continue
                if place_id and row.get("place_id") != place_id:
                    continue
                if keyword and row.get("keyword") != keyword:
                    continue
                row["place_name"] = place_map.get(row.get("place_id"), "")
                rows.append(row)
            rows.sort(key=lambda row: (row.get("date", ""), row.get("checked_at", "")), reverse=True)
            return rows

        return await asyncio.to_thread(_get_sync)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT r.*, p.place_name
            FROM rankings r
            LEFT JOIN places p
              ON r.owner_uid = p.owner_uid AND r.place_id = p.place_id
            WHERE r.owner_uid = ?
              AND r.date >= date('now', ?, 'localtime')
        """
        params = [owner_uid, f"-{days} days"]
        if place_id:
            query += " AND r.place_id = ?"
            params.append(place_id)
        if keyword:
            query += " AND r.keyword = ?"
            params.append(keyword)
        query += " ORDER BY r.date DESC, r.checked_at DESC"
        async with db.execute(query, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_latest_keyword_results(
    keyword: str,
    user_id: str | None = None,
    limit: int = 30,
):
    owner_uid = _user_id(user_id)

    if uses_firestore():
        def _get_sync():
            rows = []
            for doc in _user_doc(owner_uid).collection("keyword_results").stream():
                row = doc.to_dict()
                if row.get("keyword") == keyword:
                    rows.append(row)
            if not rows:
                return {"date": None, "results": []}
            latest_date = max(row.get("date", "") for row in rows)
            latest_rows = [
                row for row in rows
                if row.get("date") == latest_date
            ]
            latest_rows.sort(key=lambda row: row.get("rank", 999))
            return {"date": latest_date, "results": latest_rows[:limit]}

        return await asyncio.to_thread(_get_sync)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT MAX(date) AS latest_date
            FROM keyword_results
            WHERE owner_uid = ? AND keyword = ?
            """,
            (owner_uid, keyword),
        ) as cursor:
            latest_row = await cursor.fetchone()

        latest_date = latest_row["latest_date"] if latest_row else None
        if not latest_date:
            return {"date": None, "results": []}

        async with db.execute(
            """
            SELECT keyword, rank, place_id, place_name, date, checked_at
            FROM keyword_results
            WHERE owner_uid = ? AND keyword = ? AND date = ?
            ORDER BY rank ASC
            LIMIT ?
            """,
            (owner_uid, keyword, latest_date, limit),
        ) as cursor:
            return {
                "date": latest_date,
                "results": [dict(row) for row in await cursor.fetchall()],
            }


async def get_latest_rankings(user_id: str | None = None):
    """각 place_id + keyword 조합의 가장 최근 순위"""
    owner_uid = _user_id(user_id)
    if uses_firestore():
        rankings = await get_rankings(days=3650, user_id=owner_uid)
        latest = {}
        for row in rankings:
            key = (row.get("place_id"), row.get("keyword"))
            current = latest.get(key)
            if not current or (row.get("date", ""), row.get("checked_at", "")) > (
                current.get("date", ""),
                current.get("checked_at", ""),
            ):
                latest[key] = row
        return sorted(
            latest.values(),
            key=lambda row: (row.get("keyword", ""), row.get("rank") if row.get("rank", -1) > 0 else 999),
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT r.place_id, r.keyword, r.rank, r.date, r.checked_at, p.place_name
            FROM rankings r
            LEFT JOIN places p
              ON r.owner_uid = p.owner_uid AND r.place_id = p.place_id
            WHERE r.owner_uid = ?
              AND r.id IN (
                SELECT MAX(id) FROM rankings
                WHERE owner_uid = ?
                GROUP BY place_id, keyword
              )
            ORDER BY r.keyword, r.rank
        """, (owner_uid, owner_uid)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]
