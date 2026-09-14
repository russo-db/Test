"""Хранилище фермы: MongoDB в проде, SQLite для локальной разработки.

Оба бэкенда работают с одним и тем же словарём:
    user_id, coins, total_earned, mnstr, monsters, active_slot,
    missions, slots, referrals, referred_by, last_seen,
    daily_day, daily_last, eggs_board, eggs_board_unlocked, wallet, ops

Кроме игроков хранятся пополнения (deposits, ключ — хэш транзакции TON)
и заявки на вывод (withdrawals).
"""

import json
import os
import sqlite3
from typing import Optional

FIELDS = (
    "user_id", "name", "coins", "total_earned", "mnstr", "monsters",
    "active_slot", "missions", "slots", "referrals", "referred_by", "last_seen",
    "daily_day", "daily_last", "eggs_board", "eggs_board_unlocked", "wallet", "ops",
)
JSON_FIELDS = ("monsters", "missions", "eggs_board")


class SqliteStore:
    """Файловая база. Путь задаётся DB_PATH — на хостинге это должен быть том."""

    def __init__(self, path: str):
        self.path = path

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def init(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                name           TEXT    DEFAULT '',
                coins          REAL    DEFAULT 0.0,
                total_earned   REAL    DEFAULT 0.0,
                mnstr          REAL    DEFAULT 0.0,
                monsters       TEXT    DEFAULT '[]',
                active_slot    INTEGER DEFAULT 0,
                missions       TEXT    DEFAULT '[]',
                slots          INTEGER DEFAULT 3,
                referrals      INTEGER DEFAULT 0,
                referred_by    INTEGER,
                last_seen      INTEGER DEFAULT 0,
                daily_day      INTEGER DEFAULT 0,
                daily_last     INTEGER DEFAULT 0,
                eggs_board     TEXT    DEFAULT '[]',
                eggs_board_unlocked INTEGER DEFAULT 1,
                wallet         TEXT    DEFAULT '',
                ops            INTEGER DEFAULT 0
            )
            """
        )
        # Кошелёк: пополнения (ключ — хэш транзакции) и заявки на вывод.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS deposits (
                tx_hash  TEXT    PRIMARY KEY,
                user_id  INTEGER,
                amount   REAL,
                ts       INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER,
                address  TEXT,
                amount   REAL,
                status   TEXT,
                ts       INTEGER
            )
            """
        )

        # Колонки, добавленные после первых версий.
        columns = {row["name"] for row in cur.execute("PRAGMA table_info(users)")}
        for name, ddl in (
            ("slots", "INTEGER DEFAULT 3"),
            ("active_slot", "INTEGER DEFAULT 0"),
            ("mnstr", "REAL DEFAULT 0.0"),
            ("name", "TEXT DEFAULT ''"),
            ("daily_day", "INTEGER DEFAULT 0"),
            ("daily_last", "INTEGER DEFAULT 0"),
            ("eggs_board", "TEXT DEFAULT '[]'"),
            ("eggs_board_unlocked", "INTEGER DEFAULT 1"),
            ("wallet", "TEXT DEFAULT ''"),
            ("ops", "INTEGER DEFAULT 0"),
        ):
            if name not in columns:
                cur.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        conn.commit()
        conn.close()

    async def get(self, user_id: int) -> Optional[dict]:
        conn = self._connect()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
        if not row:
            return None

        doc = {key: row[key] for key in row.keys() if key in FIELDS}
        for key in JSON_FIELDS:
            try:
                doc[key] = json.loads(doc.get(key) or "[]")
            except (TypeError, ValueError):
                doc[key] = []
        return doc

    async def create(self, doc: dict) -> bool:
        """Возвращает True, если игрок действительно создан."""
        payload = dict(doc)
        for key in JSON_FIELDS:
            payload[key] = json.dumps(payload.get(key, []))

        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"INSERT OR IGNORE INTO users ({', '.join(FIELDS)}) "
            f"VALUES ({', '.join('?' * len(FIELDS))})",
            tuple(payload.get(key) for key in FIELDS),
        )
        created = cur.rowcount > 0
        conn.commit()
        conn.close()
        return created

    async def update(self, user_id: int, fields: dict):
        payload = {
            key: (json.dumps(value) if key in JSON_FIELDS else value)
            for key, value in fields.items()
        }
        assignments = ", ".join(f"{key} = ?" for key in payload)
        conn = self._connect()
        conn.execute(
            f"UPDATE users SET {assignments} WHERE user_id = ?",
            (*payload.values(), user_id),
        )
        conn.commit()
        conn.close()

    async def increment(self, user_id: int, fields: dict):
        assignments = ", ".join(f"{key} = {key} + ?" for key in fields)
        conn = self._connect()
        conn.execute(
            f"UPDATE users SET {assignments} WHERE user_id = ?",
            (*fields.values(), user_id),
        )
        conn.commit()
        conn.close()

    async def count_referrals(self, user_id: int, min_mnstr: float) -> int:
        """Сколько приглашённых уже намайнили нужный минимум Meat."""
        conn = self._connect()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE referred_by = ? AND mnstr >= ?",
            (user_id, min_mnstr),
        ).fetchone()
        conn.close()
        return int(row["n"])

    async def claim_mission(self, user_id: int, mission_id: str, gram: float, mnstr: float) -> bool:
        """Отмечает задание и начисляет награду. False — если уже было забрано."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT missions FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            claimed = json.loads(row["missions"] or "[]") if row else []
            if mission_id in claimed:
                conn.rollback()
                return False
            claimed.append(mission_id)
            cur.execute(
                """UPDATE users
                      SET missions = ?, coins = coins + ?,
                          total_earned = total_earned + ?, mnstr = mnstr + ?,
                          ops = ops + 1
                    WHERE user_id = ?""",
                (json.dumps(claimed), gram, gram, mnstr, user_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()


    async def claim_daily(self, user_id: int, today: int, day: int, gram: float,
                          mnstr: float, monster: Optional[str] = None,
                          extra_slot: bool = False) -> bool:
        """Отмечает сегодняшний вход и выдаёт награду. False — если уже забрано сегодня."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT daily_last, monsters FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row or int(row["daily_last"] or 0) == today:
                conn.rollback()
                return False

            fields = ["daily_last = ?", "daily_day = ?", "coins = coins + ?",
                      "total_earned = total_earned + ?", "mnstr = mnstr + ?",
                      "ops = ops + 1"]
            values = [today, day, gram, gram, mnstr]
            if monster:
                try:
                    farm = json.loads(row["monsters"] or "[]")
                except (TypeError, ValueError):
                    farm = []
                farm.append({"id": monster, "mined": 0.0})
                fields.append("monsters = ?")
                values.append(json.dumps(farm))
            if extra_slot:
                fields.append("slots = slots + 1")

            values.append(user_id)
            cur.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", tuple(values)
            )
            conn.commit()
            return True
        finally:
            conn.close()


    async def claim_wheel(self, user_id: int, gram: float, mnstr: float,
                          monster: Optional[str] = None, extra_slot: bool = False):
        """Начисляет приз колеса фортуны — спин всегда бесплатный и без лимита."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            fields = ["coins = coins + ?", "total_earned = total_earned + ?",
                      "mnstr = mnstr + ?", "ops = ops + 1"]
            values = [gram, gram, mnstr]

            if monster:
                cur.execute("BEGIN IMMEDIATE")
                row = cur.execute(
                    "SELECT monsters FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
                try:
                    farm = json.loads(row["monsters"] or "[]") if row else []
                except (TypeError, ValueError):
                    farm = []
                farm.append({"id": monster, "mined": 0.0})
                fields.append("monsters = ?")
                values.append(json.dumps(farm))
            if extra_slot:
                fields.append("slots = slots + 1")

            values.append(user_id)
            cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", tuple(values))
            conn.commit()
        finally:
            conn.close()

    async def credit_deposit(self, tx_hash: str, user_id: int, gram: float, ts: int) -> bool:
        """Зачисляет пополнение. False — если эта транзакция уже была учтена."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "INSERT OR IGNORE INTO deposits (tx_hash, user_id, amount, ts) VALUES (?, ?, ?, ?)",
                (tx_hash, user_id, gram, ts),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            cur.execute(
                "UPDATE users SET coins = coins + ?, ops = ops + 1 WHERE user_id = ?",
                (gram, user_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    async def request_withdraw(self, user_id: int, address: str, gram: float, ts: int) -> bool:
        """Списывает GRAM и записывает заявку. False — если не хватило баланса."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "UPDATE users SET coins = coins - ?, ops = ops + 1 WHERE user_id = ? AND coins >= ?",
                (gram, user_id, gram),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            cur.execute(
                "INSERT INTO withdrawals (user_id, address, amount, status, ts) VALUES (?, ?, ?, ?, ?)",
                (user_id, address, gram, "pending", ts),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    async def recent_operations(self, user_id: int, limit: int = 5) -> list:
        """Последние пополнения и выводы игрока — одним списком."""
        conn = self._connect()
        rows = conn.execute(
            """SELECT 'deposit' AS kind, amount, ts, '' AS status FROM deposits WHERE user_id = ?
               UNION ALL
               SELECT 'withdraw' AS kind, amount, ts, status FROM withdrawals WHERE user_id = ?
               ORDER BY ts DESC LIMIT ?""",
            (user_id, user_id, limit),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]


class MongoStore:
    """MongoDB через motor. Документ хранит списки как есть, без JSON-строк."""

    def __init__(self, uri: str, db_name: str, client=None):
        if client is None:
            from motor.motor_asyncio import AsyncIOMotorClient

            client = AsyncIOMotorClient(uri)
        self.users = client[db_name]["users"]
        self.deposits = client[db_name]["deposits"]
        self.withdrawals = client[db_name]["withdrawals"]

    async def init(self):
        await self.users.create_index("referred_by")
        await self.deposits.create_index("user_id")
        await self.withdrawals.create_index("user_id")

    async def get(self, user_id: int) -> Optional[dict]:
        doc = await self.users.find_one({"_id": user_id})
        if not doc:
            return None
        doc = dict(doc)
        doc["user_id"] = doc.pop("_id")
        return doc

    async def create(self, doc: dict) -> bool:
        payload = {key: doc.get(key) for key in FIELDS if key != "user_id"}
        payload["_id"] = doc["user_id"]
        result = await self.users.update_one(
            {"_id": doc["user_id"]}, {"$setOnInsert": payload}, upsert=True
        )
        return result.upserted_id is not None

    async def update(self, user_id: int, fields: dict):
        await self.users.update_one({"_id": user_id}, {"$set": fields})

    async def increment(self, user_id: int, fields: dict):
        await self.users.update_one({"_id": user_id}, {"$inc": fields})

    async def count_referrals(self, user_id: int, min_mnstr: float) -> int:
        """Сколько приглашённых уже намайнили нужный минимум Meat."""
        return await self.users.count_documents(
            {"referred_by": user_id, "mnstr": {"$gte": min_mnstr}}
        )

    async def claim_mission(self, user_id: int, mission_id: str, gram: float, mnstr: float) -> bool:
        """Одна атомарная операция: задание засчитывается только если его там ещё нет,
        поэтому два одновременных запроса не выдадут награду дважды."""
        result = await self.users.update_one(
            {"_id": user_id, "missions": {"$ne": mission_id}},
            {
                "$push": {"missions": mission_id},
                "$inc": {"coins": gram, "total_earned": gram, "mnstr": mnstr, "ops": 1},
            },
        )
        return result.modified_count > 0


    async def claim_daily(self, user_id: int, today: int, day: int, gram: float,
                          mnstr: float, monster: Optional[str] = None,
                          extra_slot: bool = False) -> bool:
        """Условие daily_last != today делает выдачу однократной: два одновременных
        запроса не начислят награду дважды."""
        inc = {"coins": gram, "total_earned": gram, "mnstr": mnstr, "ops": 1}
        if extra_slot:
            inc["slots"] = 1
        changes = {"$set": {"daily_last": today, "daily_day": day}, "$inc": inc}
        if monster:
            changes["$push"] = {"monsters": {"id": monster, "mined": 0.0}}

        result = await self.users.update_one(
            {"_id": user_id, "daily_last": {"$ne": today}}, changes
        )
        return result.modified_count > 0


    async def claim_wheel(self, user_id: int, gram: float, mnstr: float,
                          monster: Optional[str] = None, extra_slot: bool = False):
        """Начисляет приз колеса фортуны — спин всегда бесплатный и без лимита."""
        inc = {"coins": gram, "total_earned": gram, "mnstr": mnstr, "ops": 1}
        if extra_slot:
            inc["slots"] = 1
        changes = {"$inc": inc}
        if monster:
            changes["$push"] = {"monsters": {"id": monster, "mined": 0.0}}

        await self.users.update_one({"_id": user_id}, changes)

    async def credit_deposit(self, tx_hash: str, user_id: int, gram: float, ts: int) -> bool:
        """Хэш транзакции — это _id, поэтому одно пополнение зачислится только раз."""
        from pymongo.errors import DuplicateKeyError

        try:
            await self.deposits.insert_one(
                {"_id": tx_hash, "user_id": user_id, "amount": gram, "ts": ts}
            )
        except DuplicateKeyError:
            return False
        await self.users.update_one({"_id": user_id}, {"$inc": {"coins": gram, "ops": 1}})
        return True

    async def request_withdraw(self, user_id: int, address: str, gram: float, ts: int) -> bool:
        """Условие coins >= gram не даст увести больше, чем есть на балансе."""
        result = await self.users.update_one(
            {"_id": user_id, "coins": {"$gte": gram}},
            {"$inc": {"coins": -gram, "ops": 1}},
        )
        if result.modified_count == 0:
            return False
        await self.withdrawals.insert_one(
            {"user_id": user_id, "address": address, "amount": gram,
             "status": "pending", "ts": ts}
        )
        return True

    async def recent_operations(self, user_id: int, limit: int = 5) -> list:
        """Последние пополнения и выводы игрока — одним списком."""
        rows = []
        async for doc in self.deposits.find({"user_id": user_id}).sort("ts", -1).limit(limit):
            rows.append({"kind": "deposit", "amount": doc.get("amount", 0.0),
                         "ts": doc.get("ts", 0), "status": ""})
        async for doc in self.withdrawals.find({"user_id": user_id}).sort("ts", -1).limit(limit):
            rows.append({"kind": "withdraw", "amount": doc.get("amount", 0.0),
                         "ts": doc.get("ts", 0), "status": doc.get("status", "")})
        rows.sort(key=lambda row: row["ts"], reverse=True)
        return rows[:limit]


def make_store(client=None):
    """MongoDB, если задан MONGODB_URI, иначе файловый SQLite."""
    uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URL")
    if uri or client is not None:
        return MongoStore(uri, os.getenv("MONGODB_DB", "monstergram"), client=client)

    base = os.path.dirname(os.path.abspath(__file__))
    return SqliteStore(os.getenv("DB_PATH") or os.path.join(base, "monster_database.db"))
