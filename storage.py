"""Хранилище фермы: MongoDB в проде, SQLite для локальной разработки.

Оба бэкенда работают с одним и тем же словарём:
    user_id, coins, total_earned, mnstr, gold, monsters, farm_queue, active_slot,
    missions, slots, referrals, referred_by, last_seen,
    daily_day, daily_last, daily_cycles, eggs_board, eggs_board_unlocked, eggs_queue, wallet, ops,
    vip_tier, vip_expires_at, vip_last_meat_at

Кроме игроков хранятся пополнения (deposits, ключ — хэш транзакции TON),
заявки на вывод (withdrawals), лоты рынка (market_listings — P2P-торговля
орлами между игроками) и общий (один на всех игроков, не по-пользовательски)
счётчик лавки купца — merchant_state: {meat_bought, eagles_sold}.
"""

import json
import os
import re
import sqlite3
from typing import Optional

FIELDS = (
    "user_id", "name", "coins", "total_earned", "mnstr", "gold", "monsters", "farm_queue",
    "active_slot", "missions", "slots", "referrals", "referred_by", "last_seen",
    "daily_day", "daily_last", "daily_cycles", "eggs_board", "eggs_board_unlocked", "eggs_queue", "wallet", "ops",
    "vip_tier", "vip_expires_at", "vip_last_meat_at",
)
JSON_FIELDS = ("monsters", "farm_queue", "missions", "eggs_board", "eggs_queue")


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
                gold           REAL    DEFAULT 0.0,
                monsters       TEXT    DEFAULT '[]',
                farm_queue     TEXT    DEFAULT '[]',
                active_slot    INTEGER DEFAULT 0,
                missions       TEXT    DEFAULT '[]',
                slots          INTEGER DEFAULT 3,
                referrals      INTEGER DEFAULT 0,
                referred_by    INTEGER,
                last_seen      INTEGER DEFAULT 0,
                daily_day      INTEGER DEFAULT 0,
                daily_last     INTEGER DEFAULT 0,
                daily_cycles   INTEGER DEFAULT 0,
                eggs_board     TEXT    DEFAULT '[]',
                eggs_board_unlocked INTEGER DEFAULT 1,
                eggs_queue     TEXT    DEFAULT '[]',
                wallet         TEXT    DEFAULT '',
                ops            INTEGER DEFAULT 0,
                vip_tier       TEXT    DEFAULT '',
                vip_expires_at REAL    DEFAULT 0,
                vip_last_meat_at REAL  DEFAULT 0
            )
            """
        )
        # Купец — общая на всех игроков лавка с разовыми лимитами; строка одна
        # (id = 1), никак не привязана к конкретному user_id.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_state (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                meat_bought  REAL    DEFAULT 0,
                eagles_sold  INTEGER DEFAULT 0
            )
            """
        )
        cur.execute("INSERT OR IGNORE INTO merchant_state (id, meat_bought, eagles_sold) VALUES (1, 0, 0)")
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
        # Рынок: игрок выставляет прокачанного (7 ур.) орла редкости зелёный+ на
        # продажу за GRAM; строка живёт, пока орла не купили или не сняли с продажи.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS market_listings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id   INTEGER,
                seller_name TEXT DEFAULT '',
                monster_id  TEXT,
                price_gram  REAL,
                created_at  INTEGER
            )
            """
        )

        # Колонки, добавленные после первых версий.
        columns = {row["name"] for row in cur.execute("PRAGMA table_info(users)")}
        for name, ddl in (
            ("slots", "INTEGER DEFAULT 3"),
            ("active_slot", "INTEGER DEFAULT 0"),
            ("mnstr", "REAL DEFAULT 0.0"),
            ("gold", "REAL DEFAULT 0.0"),
            ("name", "TEXT DEFAULT ''"),
            ("daily_day", "INTEGER DEFAULT 0"),
            ("daily_last", "INTEGER DEFAULT 0"),
            ("daily_cycles", "INTEGER DEFAULT 0"),
            ("eggs_board", "TEXT DEFAULT '[]'"),
            ("eggs_board_unlocked", "INTEGER DEFAULT 1"),
            ("eggs_queue", "TEXT DEFAULT '[]'"),
            ("farm_queue", "TEXT DEFAULT '[]'"),
            ("wallet", "TEXT DEFAULT ''"),
            ("ops", "INTEGER DEFAULT 0"),
            ("vip_tier", "TEXT DEFAULT ''"),
            ("vip_expires_at", "REAL DEFAULT 0"),
            ("vip_last_meat_at", "REAL DEFAULT 0"),
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
                          extra_slot: bool = False, cycle_complete: bool = False) -> bool:
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
            if cycle_complete:
                fields.append("daily_cycles = daily_cycles + 1")
            if monster:
                try:
                    farm = json.loads(row["monsters"] or "[]")
                except (TypeError, ValueError):
                    farm = []
                farm.append({"id": monster, "next_egg_at": 0, "feed_level": 1, "feed_taps": 0})
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
                farm.append({"id": monster, "next_egg_at": 0, "feed_level": 1, "feed_taps": 0})
                fields.append("monsters = ?")
                values.append(json.dumps(farm))
            if extra_slot:
                fields.append("slots = slots + 1")

            values.append(user_id)
            cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", tuple(values))
            conn.commit()
        finally:
            conn.close()

    async def get_merchant_state(self) -> dict:
        conn = self._connect()
        row = conn.execute("SELECT meat_bought, eagles_sold FROM merchant_state WHERE id = 1").fetchone()
        conn.close()
        if not row:
            return {"meat_bought": 0.0, "eagles_sold": 0}
        return {"meat_bought": float(row["meat_bought"] or 0), "eagles_sold": int(row["eagles_sold"] or 0)}

    async def buy_merchant_meat(self, user_id: int, requested: float, limit: float,
                                 max_per_purchase: float, rate: float) -> dict:
        """Общий (на всех игроков) лимит Meat — проверяем и списываем его в той же
        транзакции, что и золото игрока, чтобы два одновременных запроса не
        продавили лимит суммарно больше положенного."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT meat_bought FROM merchant_state WHERE id = 1").fetchone()
            bought = float(row["meat_bought"] or 0) if row else 0.0
            remaining = max(0.0, limit - bought)
            amount = min(max(0.0, requested), remaining, max_per_purchase)
            if amount <= 0:
                conn.rollback()
                return {"status": "limit_reached"}

            cost = amount / rate
            urow = cur.execute("SELECT gold FROM users WHERE user_id = ?", (user_id,)).fetchone()
            gold = float(urow["gold"] or 0) if urow else 0.0
            if gold < cost:
                conn.rollback()
                return {"status": "insufficient_gold"}

            cur.execute("UPDATE merchant_state SET meat_bought = meat_bought + ? WHERE id = 1", (amount,))
            cur.execute(
                "UPDATE users SET gold = gold - ?, mnstr = mnstr + ?, ops = ops + 1 WHERE user_id = ?",
                (cost, amount, user_id),
            )
            conn.commit()
            return {"status": "ok", "amount": amount, "cost": cost}
        finally:
            conn.close()

    async def sell_merchant_eagle(self, user_id: int, slot_index: int, limit: int,
                                   price: float, common_ids) -> dict:
        """Общий (на всех игроков) лимит проданных орлов — как и с Meat, лимит и
        ферма продавца меняются в одной транзакции."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT eagles_sold FROM merchant_state WHERE id = 1").fetchone()
            sold = int(row["eagles_sold"] or 0) if row else 0
            if sold >= limit:
                conn.rollback()
                return {"status": "limit_reached"}

            urow = cur.execute("SELECT monsters FROM users WHERE user_id = ?", (user_id,)).fetchone()
            try:
                farm = json.loads(urow["monsters"] or "[]") if urow else []
            except (TypeError, ValueError):
                farm = []
            if not (0 <= slot_index < len(farm)):
                conn.rollback()
                return {"status": "not_found"}
            if farm[slot_index].get("id") not in common_ids:
                conn.rollback()
                return {"status": "wrong_tier"}
            if len(farm) <= 1:
                conn.rollback()
                return {"status": "last_eagle"}

            farm.pop(slot_index)
            cur.execute("UPDATE merchant_state SET eagles_sold = eagles_sold + 1 WHERE id = 1")
            cur.execute(
                "UPDATE users SET coins = coins + ?, total_earned = total_earned + ?, "
                "monsters = ?, active_slot = MIN(active_slot, ?), ops = ops + 1 WHERE user_id = ?",
                (price, price, json.dumps(farm), len(farm) - 1, user_id),
            )
            conn.commit()
            return {"status": "ok"}
        finally:
            conn.close()

    async def create_listing(self, seller_id: int, seller_name: str, monster_id: str,
                              feed_levels: int, price_gram: float, ts: int) -> Optional[str]:
        """Снимает с фермы первого попавшегося прокачанного (feed_levels) орла
        нужного вида и выставляет его на продажу. None — если такого орла нет."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT monsters FROM users WHERE user_id = ?", (seller_id,)).fetchone()
            try:
                farm = json.loads(row["monsters"] or "[]") if row else []
            except (TypeError, ValueError):
                farm = []
            idx = next((i for i, m in enumerate(farm)
                        if m.get("id") == monster_id and int(m.get("feed_level") or 0) >= feed_levels), None)
            if idx is None:
                conn.rollback()
                return None
            farm.pop(idx)
            cur.execute("UPDATE users SET monsters = ? WHERE user_id = ?", (json.dumps(farm), seller_id))
            cur.execute(
                "INSERT INTO market_listings (seller_id, seller_name, monster_id, price_gram, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (seller_id, seller_name, monster_id, price_gram, ts),
            )
            listing_id = cur.lastrowid
            conn.commit()
            return str(listing_id)
        finally:
            conn.close()

    async def list_listings(self, limit: int = 200) -> list:
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, seller_id, seller_name, monster_id, price_gram, created_at "
            "FROM market_listings ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        items = [dict(r) for r in rows]
        for item in items:
            item["id"] = str(item["id"])
        return items

    async def buy_listing(self, buyer_id: int, listing_id: int, feed_levels: int,
                           max_slots: int, commission: float) -> str:
        """Атомарно покупает лот: списывает GRAM с покупателя, зачисляет продавцу
        цену за вычетом комиссии рынка, добавляет орла на ферму покупателя.
        Возвращает "ok" либо код причины отказа."""
        try:
            listing_id = int(listing_id)
        except (TypeError, ValueError):
            return "not_found"
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            listing = cur.execute(
                "SELECT * FROM market_listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if not listing:
                conn.rollback()
                return "not_found"
            if int(listing["seller_id"]) == int(buyer_id):
                conn.rollback()
                return "own_listing"

            buyer = cur.execute(
                "SELECT coins, slots, monsters FROM users WHERE user_id = ?", (buyer_id,)
            ).fetchone()
            price = float(listing["price_gram"])
            if not buyer or float(buyer["coins"]) < price:
                conn.rollback()
                return "insufficient_funds"

            try:
                farm = json.loads(buyer["monsters"] or "[]")
            except (TypeError, ValueError):
                farm = []
            slots = int(buyer["slots"] or 0)
            used = len(farm)
            if used >= slots:
                if slots >= max_slots:
                    conn.rollback()
                    return "no_room"
                slots = min(max_slots, slots + 1)
            farm.append({"id": listing["monster_id"], "next_egg_at": 0,
                         "feed_level": feed_levels, "feed_taps": 0})

            cur.execute("DELETE FROM market_listings WHERE id = ?", (listing_id,))
            cur.execute(
                "UPDATE users SET coins = coins - ?, monsters = ?, slots = ?, ops = ops + 1 "
                "WHERE user_id = ?",
                (price, json.dumps(farm), slots, buyer_id),
            )
            seller_credit = price * (1 - commission)
            cur.execute(
                "UPDATE users SET coins = coins + ?, total_earned = total_earned + ?, ops = ops + 1 "
                "WHERE user_id = ?",
                (seller_credit, seller_credit, listing["seller_id"]),
            )
            conn.commit()
            return "ok"
        finally:
            conn.close()

    async def cancel_listing(self, seller_id: int, listing_id: int,
                              feed_levels: int, max_slots: int) -> str:
        """Снимает лот с продажи и возвращает орла на ферму продавца."""
        try:
            listing_id = int(listing_id)
        except (TypeError, ValueError):
            return "not_found"
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            listing = cur.execute(
                "SELECT * FROM market_listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if not listing:
                conn.rollback()
                return "not_found"
            if int(listing["seller_id"]) != int(seller_id):
                conn.rollback()
                return "not_owner"

            row = cur.execute(
                "SELECT slots, monsters FROM users WHERE user_id = ?", (seller_id,)
            ).fetchone()
            try:
                farm = json.loads(row["monsters"] or "[]") if row else []
            except (TypeError, ValueError):
                farm = []
            slots = int(row["slots"] or 0) if row else 0
            used = len(farm)
            if used >= slots:
                if slots >= max_slots:
                    conn.rollback()
                    return "no_room"
                slots = min(max_slots, slots + 1)
            farm.append({"id": listing["monster_id"], "next_egg_at": 0,
                         "feed_level": feed_levels, "feed_taps": 0})

            cur.execute("DELETE FROM market_listings WHERE id = ?", (listing_id,))
            cur.execute(
                "UPDATE users SET monsters = ?, slots = ? WHERE user_id = ?",
                (json.dumps(farm), slots, seller_id),
            )
            conn.commit()
            return "ok"
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

    # --- АДМИН-ПАНЕЛЬ ---

    def _row_to_doc(self, row) -> dict:
        doc = {key: row[key] for key in row.keys() if key in FIELDS}
        for key in JSON_FIELDS:
            try:
                doc[key] = json.loads(doc.get(key) or "[]")
            except (TypeError, ValueError):
                doc[key] = []
        return doc

    async def list_players(self, search: str = "", limit: int = 50, offset: int = 0) -> list:
        """Поиск по id (точное совпадение) или имени (подстрока)."""
        conn = self._connect()
        like = f"%{search}%"
        search = (search or "").strip()
        if search.lstrip("-").isdigit():
            rows = conn.execute(
                "SELECT * FROM users WHERE user_id = ? OR name LIKE ? "
                "ORDER BY user_id DESC LIMIT ? OFFSET ?",
                (int(search), like, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM users WHERE name LIKE ? ORDER BY user_id DESC LIMIT ? OFFSET ?",
                (like, limit, offset),
            ).fetchall()
        conn.close()
        return [self._row_to_doc(row) for row in rows]

    async def count_players(self, search: str = "") -> int:
        conn = self._connect()
        like = f"%{search}%"
        search = (search or "").strip()
        if search.lstrip("-").isdigit():
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE user_id = ? OR name LIKE ?",
                (int(search), like),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM users WHERE name LIKE ?", (like,)).fetchone()
        conn.close()
        return int(row["n"])

    async def stats(self) -> dict:
        conn = self._connect()
        row = conn.execute(
            """SELECT COUNT(*) AS players,
                      COALESCE(SUM(coins), 0) AS coins,
                      COALESCE(SUM(mnstr), 0) AS mnstr,
                      COALESCE(SUM(gold), 0) AS gold,
                      COALESCE(SUM(total_earned), 0) AS total_earned,
                      COALESCE(SUM(referrals), 0) AS referrals,
                      COALESCE(SUM(CASE WHEN wallet != '' THEN 1 ELSE 0 END), 0) AS wallets
                 FROM users"""
        ).fetchone()
        dep = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM deposits"
        ).fetchone()
        wd_pending = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM withdrawals WHERE status = 'pending'"
        ).fetchone()
        wd_paid = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM withdrawals WHERE status = 'approved'"
        ).fetchone()
        conn.close()
        return {
            "players": int(row["players"]),
            "coins": float(row["coins"]),
            "mnstr": float(row["mnstr"]),
            "gold": float(row["gold"]),
            "total_earned": float(row["total_earned"]),
            "referrals": int(row["referrals"]),
            "wallets": int(row["wallets"]),
            "deposits_count": int(dep["n"]),
            "deposits_total": float(dep["total"]),
            "withdrawals_pending_count": int(wd_pending["n"]),
            "withdrawals_pending_total": float(wd_pending["total"]),
            "withdrawals_paid_count": int(wd_paid["n"]),
            "withdrawals_paid_total": float(wd_paid["total"]),
        }

    async def list_withdrawals(self, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> list:
        conn = self._connect()
        if status:
            rows = conn.execute(
                "SELECT * FROM withdrawals WHERE status = ? ORDER BY ts DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM withdrawals ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    async def set_withdrawal_status(self, wd_id: int, status: str, refund: bool = False) -> bool:
        """Меняет статус заявки (только если ещё pending); при отказе возвращает GRAM."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT * FROM withdrawals WHERE id = ?", (wd_id,)).fetchone()
            if not row or row["status"] != "pending":
                conn.rollback()
                return False
            cur.execute("UPDATE withdrawals SET status = ? WHERE id = ?", (status, wd_id))
            if refund:
                cur.execute(
                    "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                    (row["amount"], row["user_id"]),
                )
            conn.commit()
            return True
        finally:
            conn.close()


class MongoStore:
    """MongoDB через motor. Документ хранит списки как есть, без JSON-строк."""

    def __init__(self, uri: str, db_name: str, client=None):
        if client is None:
            from motor.motor_asyncio import AsyncIOMotorClient

            client = AsyncIOMotorClient(uri)
        self.users = client[db_name]["users"]
        self.deposits = client[db_name]["deposits"]
        self.withdrawals = client[db_name]["withdrawals"]
        self.market = client[db_name]["market_listings"]
        self.merchant = client[db_name]["merchant_state"]

    async def init(self):
        await self.users.create_index("referred_by")
        await self.deposits.create_index("user_id")
        await self.withdrawals.create_index("user_id")
        await self.market.create_index("seller_id")
        # Купец — общая на всех игроков лавка с разовыми лимитами; документ один
        # (_id = "global"), никак не привязан к конкретному user_id.
        await self.merchant.update_one(
            {"_id": "global"}, {"$setOnInsert": {"meat_bought": 0, "eagles_sold": 0}}, upsert=True
        )

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
                          extra_slot: bool = False, cycle_complete: bool = False) -> bool:
        """Условие daily_last != today делает выдачу однократной: два одновременных
        запроса не начислят награду дважды."""
        inc = {"coins": gram, "total_earned": gram, "mnstr": mnstr, "ops": 1}
        if extra_slot:
            inc["slots"] = 1
        if cycle_complete:
            inc["daily_cycles"] = 1
        changes = {"$set": {"daily_last": today, "daily_day": day}, "$inc": inc}
        if monster:
            changes["$push"] = {"monsters": {"id": monster, "next_egg_at": 0, "feed_level": 1, "feed_taps": 0}}

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
            changes["$push"] = {"monsters": {"id": monster, "next_egg_at": 0, "feed_level": 1, "feed_taps": 0}}

        await self.users.update_one({"_id": user_id}, changes)

    async def get_merchant_state(self) -> dict:
        doc = await self.merchant.find_one({"_id": "global"}) or {}
        return {"meat_bought": float(doc.get("meat_bought") or 0), "eagles_sold": int(doc.get("eagles_sold") or 0)}

    async def buy_merchant_meat(self, user_id: int, requested: float, limit: float,
                                 max_per_purchase: float, rate: float) -> dict:
        """Общий (на всех игроков) лимит Meat: сперва атомарно резервируем место
        в лимите, затем списываем золото игрока; если золота не хватило —
        возвращаем резерв обратно (двухфазный подход, раз Mongo здесь без
        многодокументных транзакций, как и в buy_listing)."""
        state = await self.merchant.find_one({"_id": "global"}) or {}
        bought = float(state.get("meat_bought") or 0)
        remaining = max(0.0, limit - bought)
        amount = min(max(0.0, requested), remaining, max_per_purchase)
        if amount <= 0:
            return {"status": "limit_reached"}

        reserve = await self.merchant.update_one(
            {"_id": "global", "meat_bought": {"$lte": limit - amount}},
            {"$inc": {"meat_bought": amount}},
        )
        if reserve.modified_count == 0:
            return {"status": "limit_reached"}

        cost = amount / rate
        charge = await self.users.update_one(
            {"_id": user_id, "gold": {"$gte": cost}},
            {"$inc": {"gold": -cost, "mnstr": amount, "ops": 1}},
        )
        if charge.modified_count == 0:
            await self.merchant.update_one({"_id": "global"}, {"$inc": {"meat_bought": -amount}})
            return {"status": "insufficient_gold"}
        return {"status": "ok", "amount": amount, "cost": cost}

    async def sell_merchant_eagle(self, user_id: int, slot_index: int, limit: int,
                                   price: float, common_ids) -> dict:
        """Общий (на всех игроков) лимит проданных орлов — тот же двухфазный
        подход: резерв лимита, потом ферма продавца по оптимистичной блокировке
        (полное совпадение monsters), с откатом резерва при конфликте."""
        state = await self.merchant.find_one({"_id": "global"}) or {}
        sold = int(state.get("eagles_sold") or 0)
        if sold >= limit:
            return {"status": "limit_reached"}

        doc = await self.users.find_one({"_id": user_id}, {"monsters": 1, "active_slot": 1})
        farm = list((doc or {}).get("monsters") or [])
        active_slot = int((doc or {}).get("active_slot") or 0)
        if not (0 <= slot_index < len(farm)):
            return {"status": "not_found"}
        if farm[slot_index].get("id") not in common_ids:
            return {"status": "wrong_tier"}
        if len(farm) <= 1:
            return {"status": "last_eagle"}

        reserve = await self.merchant.update_one(
            {"_id": "global", "eagles_sold": {"$lte": limit - 1}},
            {"$inc": {"eagles_sold": 1}},
        )
        if reserve.modified_count == 0:
            return {"status": "limit_reached"}

        original = farm[:]
        new_farm = farm[:slot_index] + farm[slot_index + 1:]
        result = await self.users.update_one(
            {"_id": user_id, "monsters": original},
            {
                "$set": {"monsters": new_farm, "active_slot": min(active_slot, len(new_farm) - 1)},
                "$inc": {"coins": price, "total_earned": price, "ops": 1},
            },
        )
        if result.modified_count == 0:
            await self.merchant.update_one({"_id": "global"}, {"$inc": {"eagles_sold": -1}})
            return {"status": "conflict"}
        return {"status": "ok"}

    async def create_listing(self, seller_id: int, seller_name: str, monster_id: str,
                              feed_levels: int, price_gram: float, ts: int) -> Optional[str]:
        """Снимает с фермы первого попавшегося прокачанного (feed_levels) орла
        нужного вида и выставляет его на продажу. None — если такого орла нет
        (или ферму поменяли параллельно — оптимистичная блокировка по monsters)."""
        doc = await self.users.find_one({"_id": seller_id}, {"monsters": 1})
        farm = list((doc or {}).get("monsters") or [])
        idx = next((i for i, m in enumerate(farm)
                    if m.get("id") == monster_id and int(m.get("feed_level") or 0) >= feed_levels), None)
        if idx is None:
            return None
        original = farm[:]
        farm.pop(idx)
        result = await self.users.update_one(
            {"_id": seller_id, "monsters": original}, {"$set": {"monsters": farm}}
        )
        if result.modified_count == 0:
            return None
        listing = {
            "seller_id": seller_id, "seller_name": seller_name,
            "monster_id": monster_id, "price_gram": price_gram, "created_at": ts,
        }
        result = await self.market.insert_one(listing)
        return str(result.inserted_id)

    async def list_listings(self, limit: int = 200) -> list:
        cursor = self.market.find().sort("created_at", -1).limit(limit)
        items = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            items.append(doc)
        return items

    async def buy_listing(self, buyer_id: int, listing_id: str, feed_levels: int,
                           max_slots: int, commission: float) -> str:
        """Атомарно покупает лот: списывает GRAM с покупателя, зачисляет продавцу
        цену за вычетом комиссии рынка, добавляет орла на ферму покупателя.
        Возвращает "ok" либо код причины отказа."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"

        listing = await self.market.find_one_and_delete({"_id": oid})
        if not listing:
            return "not_found"
        if int(listing["seller_id"]) == int(buyer_id):
            # Отменять покупку не нужно — лот просто возвращаем на место.
            await self.market.insert_one(listing)
            return "own_listing"

        price = float(listing["price_gram"])
        buyer = await self.users.find_one({"_id": buyer_id}, {"coins": 1, "slots": 1, "monsters": 1})
        if not buyer or float(buyer.get("coins") or 0) < price:
            await self.market.insert_one(listing)
            return "insufficient_funds"

        farm = list(buyer.get("monsters") or [])
        slots = int(buyer.get("slots") or 0)
        used = len(farm)
        if used >= slots:
            if slots >= max_slots:
                await self.market.insert_one(listing)
                return "no_room"
            slots = min(max_slots, slots + 1)
        farm.append({"id": listing["monster_id"], "next_egg_at": 0,
                     "feed_level": feed_levels, "feed_taps": 0})

        result = await self.users.update_one(
            {"_id": buyer_id, "coins": {"$gte": price}},
            {"$set": {"monsters": farm, "slots": slots}, "$inc": {"coins": -price, "ops": 1}},
        )
        if result.modified_count == 0:
            # Баланс утёк параллельным запросом — откатываем лот обратно.
            await self.market.insert_one(listing)
            return "insufficient_funds"

        seller_credit = price * (1 - commission)
        await self.users.update_one(
            {"_id": listing["seller_id"]},
            {"$inc": {"coins": seller_credit, "total_earned": seller_credit, "ops": 1}},
        )
        return "ok"

    async def cancel_listing(self, seller_id: int, listing_id: str,
                              feed_levels: int, max_slots: int) -> str:
        """Снимает лот с продажи и возвращает орла на ферму продавца."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"

        listing = await self.market.find_one({"_id": oid})
        if not listing:
            return "not_found"
        if int(listing["seller_id"]) != int(seller_id):
            return "not_owner"

        deleted = await self.market.find_one_and_delete({"_id": oid, "seller_id": seller_id})
        if not deleted:
            return "not_found"

        row = await self.users.find_one({"_id": seller_id}, {"slots": 1, "monsters": 1})
        farm = list((row or {}).get("monsters") or [])
        slots = int((row or {}).get("slots") or 0)
        used = len(farm)
        if used >= slots:
            if slots >= max_slots:
                await self.market.insert_one(deleted)
                return "no_room"
            slots = min(max_slots, slots + 1)
        farm.append({"id": deleted["monster_id"], "next_egg_at": 0,
                     "feed_level": feed_levels, "feed_taps": 0})
        await self.users.update_one({"_id": seller_id}, {"$set": {"monsters": farm, "slots": slots}})
        return "ok"

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

    # --- АДМИН-ПАНЕЛЬ ---

    def _search_query(self, search: str) -> dict:
        search = (search or "").strip()
        if not search:
            return {}
        clauses = [{"name": {"$regex": re.escape(search), "$options": "i"}}]
        if search.lstrip("-").isdigit():
            clauses.append({"_id": int(search)})
        return {"$or": clauses}

    async def list_players(self, search: str = "", limit: int = 50, offset: int = 0) -> list:
        docs = []
        cursor = self.users.find(self._search_query(search)).sort("_id", -1).skip(offset).limit(limit)
        async for doc in cursor:
            doc = dict(doc)
            doc["user_id"] = doc.pop("_id")
            docs.append(doc)
        return docs

    async def count_players(self, search: str = "") -> int:
        return await self.users.count_documents(self._search_query(search))

    async def stats(self) -> dict:
        pipeline = [{"$group": {
            "_id": None,
            "players": {"$sum": 1},
            "coins": {"$sum": "$coins"},
            "mnstr": {"$sum": "$mnstr"},
            "gold": {"$sum": "$gold"},
            "total_earned": {"$sum": "$total_earned"},
            "referrals": {"$sum": "$referrals"},
            "wallets": {"$sum": {"$cond": [{"$ne": ["$wallet", ""]}, 1, 0]}},
        }}]
        agg = await self.users.aggregate(pipeline).to_list(1)
        base = agg[0] if agg else {}

        async def _sum(collection, match=None):
            stages = ([{"$match": match}] if match else []) + [
                {"$group": {"_id": None, "n": {"$sum": 1}, "total": {"$sum": "$amount"}}}
            ]
            result = await collection.aggregate(stages).to_list(1)
            return result[0] if result else {"n": 0, "total": 0}

        dep = await _sum(self.deposits)
        wd_pending = await _sum(self.withdrawals, {"status": "pending"})
        wd_paid = await _sum(self.withdrawals, {"status": "approved"})

        return {
            "players": int(base.get("players", 0)),
            "coins": float(base.get("coins", 0) or 0),
            "mnstr": float(base.get("mnstr", 0) or 0),
            "gold": float(base.get("gold", 0) or 0),
            "total_earned": float(base.get("total_earned", 0) or 0),
            "referrals": int(base.get("referrals", 0) or 0),
            "wallets": int(base.get("wallets", 0) or 0),
            "deposits_count": int(dep.get("n", 0)),
            "deposits_total": float(dep.get("total", 0) or 0),
            "withdrawals_pending_count": int(wd_pending.get("n", 0)),
            "withdrawals_pending_total": float(wd_pending.get("total", 0) or 0),
            "withdrawals_paid_count": int(wd_paid.get("n", 0)),
            "withdrawals_paid_total": float(wd_paid.get("total", 0) or 0),
        }

    async def list_withdrawals(self, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> list:
        query = {"status": status} if status else {}
        docs = []
        cursor = self.withdrawals.find(query).sort("ts", -1).skip(offset).limit(limit)
        async for doc in cursor:
            doc = dict(doc)
            doc["id"] = str(doc.pop("_id"))
            docs.append(doc)
        return docs

    async def set_withdrawal_status(self, wd_id: str, status: str, refund: bool = False) -> bool:
        from bson import ObjectId

        oid = ObjectId(wd_id)
        doc = await self.withdrawals.find_one({"_id": oid, "status": "pending"})
        if not doc:
            return False
        await self.withdrawals.update_one({"_id": oid}, {"$set": {"status": status}})
        if refund:
            await self.users.update_one({"_id": doc["user_id"]}, {"$inc": {"coins": doc["amount"]}})
        return True


def _mask_uri(uri: str) -> str:
    """Прячет пароль из строки подключения перед выводом в лог."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", uri)


def make_store(client=None):
    """MongoDB, если задан MONGODB_URI, иначе файловый SQLite."""
    uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URL")
    if uri or client is not None:
        db_name = os.getenv("MONGODB_DB", "monstergram")
        print(f"[storage] backend = MongoDB, db = {db_name!r}, uri = {_mask_uri(uri) if uri else '(client passed in)'}")
        return MongoStore(uri, db_name, client=client)

    base = os.path.dirname(os.path.abspath(__file__))
    path = os.getenv("DB_PATH") or os.path.join(base, "monster_database.db")
    print(f"[storage] backend = SQLite, path = {path!r} (MONGODB_URI/MONGO_URL не заданы)")
    return SqliteStore(path)
