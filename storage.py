"""Хранилище фермы: единственный бэкенд — MongoDB.

Документ игрока:
    user_id, coins, total_earned, mnstr, gold, monsters, farm_queue, active_slot,
    missions, slots, referrals, referred_by, last_seen,
    daily_day, daily_last, daily_cycles, eggs_board, eggs_board_unlocked, eggs_queue, wallet, ops,
    vip_tier, vip_expires_at, vip_last_meat_at, wheel_day, wheel_spins_today,
    nest_miners, nest_particles, nest_last_claim, nest_inventory, nest_equipped
    (Гнездо Воинов: добыча частичек, крафт/улучшение снаряжения, экипировка —
    личные для каждого игрока; nest_last_claim — точка отсчёта непрерывного
    накопления частичек, см. nest_pending_particles в main.py)
    pvp_rating (Арена: рейтинг для Топ-100, старт 1000, +25/-15 за победу/поражение)
    pvp_energy, pvp_energy_day (Арена: энергия на вход в бой, потолок 10,
    пополняется раз в UTC-сутки; day — номер суток последнего пополнения)

Кроме игроков хранятся пополнения (deposits, ключ — хэш транзакции TON),
заявки на вывод (withdrawals), лоты рынка (market_listings — P2P-торговля
орлами между игроками), лоты рынка снаряжения (equip_listings — P2P-торговля
предметами Кузницы по грейдам) и рынка ресурсов (resource_listings —
P2P-торговля целыми Небесными Осколками и целыми частичками), общий (один
на всех игроков, не по-пользовательски) счётчик лавки купца —
merchant_state: {meat_bought, eagles_sold} — и точно так же общий
кулдаун/суточный лимит покупки Небесного Осколка —
nest_state: {cooldown_until, day, bought_today}.
"""

import os
import random
import re
from typing import Optional

FIELDS = (
    "user_id", "name", "coins", "total_earned", "mnstr", "gold", "monsters", "farm_queue",
    "active_slot", "missions", "slots", "referrals", "referred_by", "last_seen",
    "daily_day", "daily_last", "daily_cycles", "eggs_board", "eggs_board_unlocked", "eggs_queue", "wallet", "ops",
    "vip_tier", "vip_expires_at", "vip_last_meat_at", "wheel_day", "wheel_spins_today",
    "nest_miners", "nest_particles", "nest_last_claim", "nest_inventory", "nest_equipped", "pvp_rating",
    "pvp_energy", "pvp_energy_day",
)


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
        self.equip_market = client[db_name]["equip_listings"]
        self.resource_market = client[db_name]["resource_listings"]
        self.merchant = client[db_name]["merchant_state"]
        self.nest_global = client[db_name]["nest_state"]

    async def init(self):
        await self.users.create_index("referred_by")
        await self.deposits.create_index("user_id")
        await self.withdrawals.create_index("user_id")
        await self.market.create_index("seller_id")
        await self.equip_market.create_index("seller_id")
        await self.resource_market.create_index("seller_id")
        # Купец — общая на всех игроков лавка с разовыми лимитами; документ один
        # (_id = "global"), никак не привязан к конкретному user_id.
        await self.merchant.update_one(
            {"_id": "global"}, {"$setOnInsert": {"meat_bought": 0, "eagles_sold": 0}}, upsert=True
        )
        # Небесный Осколок — тот же принцип: один общий документ на всех
        # игроков сразу (кулдаун и суточный лимит покупки не персональные).
        await self.nest_global.update_one(
            {"_id": "global"}, {"$setOnInsert": {"cooldown_until": 0, "day": 0, "bought_today": 0}}, upsert=True
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

    async def cas_update(self, user_id: int, fields: dict, expected_ops: int) -> bool:
        """Compare-and-swap: пишет fields (и сам увеличивает ops), только если
        документ с момента чтения не изменился (ops всё ещё expected_ops).
        Общий примитив атомарности для всех действий фермы/яиц/VIP — те же
        гарантии, что и у ops-проверки в /api/save, но применяются к каждому
        отдельному действию, а не только к целиком клиентскому сохранению.
        False — параллельное действие уже сдвинуло ops, вызывающая сторона
        должна перечитать документ и повторить попытку с начала."""
        fields = dict(fields)
        fields["ops"] = expected_ops + 1
        result = await self.users.update_one(
            {"_id": user_id, "ops": expected_ops}, {"$set": fields}
        )
        return result.modified_count > 0


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



    async def spend_wheel_spin(self, user_id: int, today: int, cheap_spins: int,
                                cheap_cost: float, expensive_cost: float) -> dict:
        """Списывает стоимость прокрута колеса — двухфазный подход (сперва читаем
        состояние, потом обновляем с проверкой в фильтре), как и в мерчанте:
        два одновременных прокрута не смогут списать по заниженной (устаревшей)
        цене одновременно."""
        doc = await self.users.find_one({"_id": user_id}, {"coins": 1, "wheel_day": 1, "wheel_spins_today": 1})
        if not doc:
            return {"status": "not_found"}

        coins = float(doc.get("coins") or 0)
        stored_day = int(doc.get("wheel_day") or 0)
        stored_spins = int(doc.get("wheel_spins_today") or 0)
        spins_today = stored_spins if stored_day == today else 0
        attempt = spins_today + 1
        cost = cheap_cost if attempt <= cheap_spins else expensive_cost
        if coins < cost:
            return {"status": "insufficient_gram", "cost": cost}

        if stored_day == today:
            filt = {"_id": user_id, "coins": {"$gte": cost}, "wheel_day": today, "wheel_spins_today": stored_spins}
            update = {"$inc": {"coins": -cost, "wheel_spins_today": 1, "ops": 1}}
        else:
            filt = {"_id": user_id, "coins": {"$gte": cost}, "wheel_day": {"$ne": today}}
            update = {"$set": {"wheel_day": today, "wheel_spins_today": 1}, "$inc": {"coins": -cost, "ops": 1}}

        result = await self.users.update_one(filt, update)
        if result.modified_count == 0:
            return {"status": "conflict"}
        return {"status": "ok", "cost": cost, "attempt": attempt}

    async def get_merchant_state(self) -> dict:
        doc = await self.merchant.find_one({"_id": "global"}) or {}
        return {"meat_bought": float(doc.get("meat_bought") or 0), "eagles_sold": int(doc.get("eagles_sold") or 0)}

    async def reset_merchant_state(self) -> dict:
        await self.merchant.update_one(
            {"_id": "global"}, {"$set": {"meat_bought": 0, "eagles_sold": 0}}, upsert=True
        )
        return {"meat_bought": 0.0, "eagles_sold": 0}

    async def get_nest_state(self, today: int, daily_limit: int) -> dict:
        doc = await self.nest_global.find_one({"_id": "global"}) or {}
        stored_day = int(doc.get("day") or 0)
        bought_today = int(doc.get("bought_today") or 0) if stored_day == today else 0
        return {
            "cooldown_until": float(doc.get("cooldown_until") or 0),
            "bought_today": bought_today,
            "daily_limit": daily_limit,
        }

    async def buy_nest_shard(self, user_id: int, now: float, today: int, price_gram: float,
                              cooldown_seconds: float, daily_limit: int, new_last_claim: float) -> dict:
        """Небесный Осколок — общий (не персональный) ресурс: доступен строго
        1 за раз НА ВСЕХ игроков, а суточный лимит покупок тоже один общий
        счётчик (обнуляется по UTC-суткам). Двухфазный подход, как и у
        лимитов купца: сперва атомарно резервируем покупку в общем
        состоянии (кулдаун сдвигается для всех сразу), потом списываем GRAM
        у покупателя и добавляем ЕМУ ОДНОМУ новый осколок в Кузницу; при
        нехватке средств — откатываем резерв, чтобы не сжигать чужой
        кулдаун и лимит впустую."""
        state = await self.nest_global.find_one({"_id": "global"}) or {}
        cooldown_until = float(state.get("cooldown_until") or 0)
        if now < cooldown_until:
            return {"status": "cooldown", "cooldown_until": cooldown_until}

        stored_day = int(state.get("day") or 0)
        bought_today = int(state.get("bought_today") or 0) if stored_day == today else 0
        if bought_today >= daily_limit:
            return {"status": "daily_limit"}

        new_cooldown = now + cooldown_seconds
        reserve = await self.nest_global.update_one(
            {"_id": "global", "cooldown_until": cooldown_until},
            {"$set": {"cooldown_until": new_cooldown, "day": today, "bought_today": bought_today + 1}},
        )
        if reserve.modified_count == 0:
            return {"status": "conflict"}

        miner = {"id": f"{user_id}-{int(now * 1000)}"}
        charge = await self.users.update_one(
            {"_id": user_id, "coins": {"$gte": price_gram}},
            {
                "$inc": {"coins": -price_gram, "ops": 1},
                "$push": {"nest_miners": miner},
                "$set": {"nest_last_claim": new_last_claim},
            },
        )
        if charge.modified_count == 0:
            # Откат безопасен: пока наш резерв держит общий кулдаун, никто
            # другой купить не мог — конкурентных изменений между резервом
            # и этим откатом быть не может.
            await self.nest_global.update_one(
                {"_id": "global"},
                {"$set": {"cooldown_until": cooldown_until, "day": stored_day, "bought_today": bought_today}},
            )
            return {"status": "insufficient_gram"}

        return {"status": "ok", "cooldown_until": new_cooldown, "bought_today": bought_today + 1}

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
                                   price: float, common_ids, feed_levels: int) -> dict:
        """Общий (на всех игроков) лимит проданных орлов — тот же двухфазный
        подход: резерв лимита, потом ферма продавца по оптимистичной блокировке
        (полное совпадение monsters И farm_queue), с откатом резерва при
        конфликте. Купец берёт только полностью откормленных (feed_level >=
        feed_levels) обычных орлов. Освободившийся слот сразу добирает орла
        из очереди (farm_queue) — иначе слот пустует, пока клиент не
        перезагрузит ферму."""
        state = await self.merchant.find_one({"_id": "global"}) or {}
        sold = int(state.get("eagles_sold") or 0)
        if sold >= limit:
            return {"status": "limit_reached"}

        doc = await self.users.find_one(
            {"_id": user_id}, {"monsters": 1, "active_slot": 1, "farm_queue": 1, "slots": 1}
        )
        farm = list((doc or {}).get("monsters") or [])
        active_slot = int((doc or {}).get("active_slot") or 0)
        original_queue = list((doc or {}).get("farm_queue") or [])
        slots_count = int((doc or {}).get("slots") or 0)
        if not (0 <= slot_index < len(farm)):
            return {"status": "not_found"}
        if farm[slot_index].get("id") not in common_ids:
            return {"status": "wrong_tier"}
        if int(farm[slot_index].get("feed_level") or 0) < feed_levels:
            return {"status": "not_fed"}
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
        new_queue = original_queue[:]
        while len(new_farm) < slots_count and new_queue:
            new_farm.append(new_queue.pop(0))
        result = await self.users.update_one(
            {"_id": user_id, "monsters": original, "farm_queue": original_queue},
            {
                "$set": {
                    "monsters": new_farm, "farm_queue": new_queue,
                    "active_slot": min(active_slot, len(new_farm) - 1),
                },
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

    # --- РЫНОК СНАРЯЖЕНИЯ: P2P-торговля предметами Кузницы по грейдам.
    # Полностью самодостаточен внутри storage.py (в отличие от рынка
    # ресурсов ниже) — nest_inventory это просто счётчики по (тип, грейд),
    # без побочных игровых формул вроде ставки накопления частичек. ---

    async def create_equip_listing(self, seller_id: int, seller_name: str, item_type: str,
                                    grade: str, price_gram: float, ts: int) -> Optional[str]:
        """Атомарно списывает 1 шт. предмета из nest_inventory[item_type][grade]
        и создаёт лот; None — если такого предмета не хватает."""
        field = f"nest_inventory.{item_type}.{grade}"
        result = await self.users.update_one(
            {"_id": seller_id, field: {"$gte": 1}},
            {"$inc": {field: -1, "ops": 1}},
        )
        if result.modified_count == 0:
            return None
        listing = {
            "seller_id": seller_id, "seller_name": seller_name,
            "item_type": item_type, "grade": grade, "price_gram": price_gram, "created_at": ts,
        }
        result = await self.equip_market.insert_one(listing)
        return str(result.inserted_id)

    async def list_equip_listings(self, limit: int = 200) -> list:
        cursor = self.equip_market.find().sort("created_at", -1).limit(limit)
        items = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            items.append(doc)
        return items

    async def buy_equip_listing(self, buyer_id: int, listing_id: str, commission: float) -> str:
        """Тот же claim-затем-проверки-затем-откат порядок, что и у
        buy_listing для орлов, но выдача — простой $inc по nest_inventory,
        без капасити фермы (снаряжение не занимает слоты)."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"

        listing = await self.equip_market.find_one_and_delete({"_id": oid})
        if not listing:
            return "not_found"
        if int(listing["seller_id"]) == int(buyer_id):
            await self.equip_market.insert_one(listing)
            return "own_listing"

        price = float(listing["price_gram"])
        result = await self.users.update_one(
            {"_id": buyer_id, "coins": {"$gte": price}},
            {"$inc": {"coins": -price, "ops": 1}},
        )
        if result.modified_count == 0:
            await self.equip_market.insert_one(listing)
            return "insufficient_funds"

        field = f"nest_inventory.{listing['item_type']}.{listing['grade']}"
        await self.users.update_one({"_id": buyer_id}, {"$inc": {field: 1}})

        seller_credit = price * (1 - commission)
        await self.users.update_one(
            {"_id": listing["seller_id"]},
            {"$inc": {"coins": seller_credit, "total_earned": seller_credit, "ops": 1}},
        )
        return "ok"

    async def cancel_equip_listing(self, seller_id: int, listing_id: str) -> str:
        """Снимает лот снаряжения и возвращает предмет в инвентарь продавца."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"

        listing = await self.equip_market.find_one_and_delete({"_id": oid, "seller_id": seller_id})
        if not listing:
            exists = await self.equip_market.find_one({"_id": oid})
            return "not_owner" if exists else "not_found"

        field = f"nest_inventory.{listing['item_type']}.{listing['grade']}"
        await self.users.update_one({"_id": seller_id}, {"$inc": {field: 1}})
        return "ok"

    # --- РЫНОК РЕСУРСОВ: P2P-торговля целыми Небесными Осколками и целыми
    # частичками. В отличие от рынка снаряжения выше, списание/зачисление
    # самого ресурса делает НЕ этот класс, а main.py._adjust_user_resource —
    # Осколки завязаны на игровую формулу ставки накопления частичек
    # (nest_settle_particles), которой в storage.py нет и быть не должно.
    # Эти методы отвечают только за лот и GRAM, ровно как buy_listing/
    # cancel_listing выше отвечают только за орла и GRAM. ---

    async def create_resource_listing(self, seller_id: int, seller_name: str, resource: str,
                                       amount: int, price_gram: float, ts: int) -> str:
        """Просто создаёт лот — ресурс у продавца уже списан вызывающим
        кодом (main.py._adjust_user_resource) ДО этого вызова."""
        listing = {
            "seller_id": seller_id, "seller_name": seller_name, "resource": resource,
            "amount": amount, "price_gram": price_gram, "created_at": ts,
        }
        result = await self.resource_market.insert_one(listing)
        return str(result.inserted_id)

    async def list_resource_listings(self, limit: int = 200) -> list:
        cursor = self.resource_market.find().sort("created_at", -1).limit(limit)
        items = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            items.append(doc)
        return items

    async def claim_resource_listing_for_buy(self, buyer_id: int, listing_id: str):
        """Забирает лот и сразу списывает GRAM с покупателя — атомарно,
        как и в buy_listing/buy_equip_listing. Возвращает СЛОВАРЬ лота при
        успехе (ресурс покупателю ещё не зачислен — это отдельный шаг в
        main.py, см. _adjust_user_resource) либо код отказа: "not_found",
        "own_listing", "insufficient_funds"."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"

        listing = await self.resource_market.find_one_and_delete({"_id": oid})
        if not listing:
            return "not_found"
        if int(listing["seller_id"]) == int(buyer_id):
            await self.resource_market.insert_one(listing)
            return "own_listing"

        price = float(listing["price_gram"])
        result = await self.users.update_one(
            {"_id": buyer_id, "coins": {"$gte": price}},
            {"$inc": {"coins": -price, "ops": 1}},
        )
        if result.modified_count == 0:
            await self.resource_market.insert_one(listing)
            return "insufficient_funds"

        listing["id"] = str(listing.pop("_id"))
        return listing

    async def refund_failed_resource_purchase(self, buyer_id: int, listing: dict) -> None:
        """Откат claim_resource_listing_for_buy, если main.py не смог зачислить
        ресурс покупателю после списания GRAM (гонка на 5 попыток) —
        возвращает GRAM и восстанавливает лот, чтобы деньги не пропали
        без товара."""
        price = float(listing["price_gram"])
        await self.users.update_one({"_id": buyer_id}, {"$inc": {"coins": price, "ops": 1}})
        await self.restore_resource_listing(listing)

    async def restore_resource_listing(self, listing: dict) -> None:
        doc = dict(listing)
        listing_id = doc.pop("id", None)
        if listing_id is not None:
            from bson import ObjectId
            doc["_id"] = ObjectId(listing_id)
        await self.resource_market.insert_one(doc)

    async def credit_resource_seller(self, seller_id: int, price_gram: float, commission: float) -> None:
        seller_credit = price_gram * (1 - commission)
        await self.users.update_one(
            {"_id": seller_id},
            {"$inc": {"coins": seller_credit, "total_earned": seller_credit, "ops": 1}},
        )

    async def take_own_resource_listing(self, seller_id: int, listing_id: str):
        """Снимает СВОЙ лот ресурса с продажи — как cancel_listing/
        cancel_equip_listing, возвращает словарь лота или код отказа
        ("not_found"/"not_owner"); зачисление ресурса продавцу обратно —
        отдельный шаг в main.py (_adjust_user_resource)."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"

        listing = await self.resource_market.find_one_and_delete({"_id": oid, "seller_id": seller_id})
        if not listing:
            exists = await self.resource_market.find_one({"_id": oid})
            return "not_owner" if exists else "not_found"

        listing["id"] = str(listing.pop("_id"))
        return listing

    async def credit_deposit(self, tx_hash: str, user_id: int, gram: float, ts: int,
                              referrer_id: Optional[int] = None, referral_gram: float = 0.0) -> bool:
        """Хэш транзакции — это _id, поэтому одно пополнение зачислится только раз.
        Реферальный процент начисляется после — дубликат транзакции отсекается
        ещё на insert_one, так что бонус тоже не задвоится."""
        from pymongo.errors import DuplicateKeyError

        try:
            await self.deposits.insert_one(
                {"_id": tx_hash, "user_id": user_id, "amount": gram, "ts": ts}
            )
        except DuplicateKeyError:
            return False
        await self.users.update_one({"_id": user_id}, {"$inc": {"coins": gram, "ops": 1}})
        if referrer_id and referral_gram > 0:
            await self.users.update_one(
                {"_id": referrer_id}, {"$inc": {"coins": referral_gram, "ops": 1}}
            )
        return True

    async def request_withdraw(self, user_id: int, address: str, gram: float,
                                payout: float, ts: int) -> bool:
        """Условие coins >= gram не даст увести больше, чем есть на балансе.
        payout — сколько TON реально уйдёт игроку после комиссии за вывод."""
        result = await self.users.update_one(
            {"_id": user_id, "coins": {"$gte": gram}},
            {"$inc": {"coins": -gram, "ops": 1}},
        )
        if result.modified_count == 0:
            return False
        await self.withdrawals.insert_one(
            {"user_id": user_id, "address": address, "amount": gram, "payout": payout,
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

    async def list_referrals(self, referrer_id: int) -> list:
        """Друзья, приглашённые этим игроком, и сумма их пополнений — реальный
        реферальный бонус считается от неё же (см. REFERRAL_SHARE), поэтому
        отдельный счётчик бонуса не хранится, а пересчитывается на лету."""
        friends = []
        async for doc in self.users.find({"referred_by": referrer_id}, {"name": 1}):
            friends.append({"user_id": doc["_id"], "name": doc.get("name") or ""})
        if not friends:
            return []

        friend_ids = [f["user_id"] for f in friends]
        pipeline = [
            {"$match": {"user_id": {"$in": friend_ids}}},
            {"$group": {"_id": "$user_id", "total": {"$sum": "$amount"}}},
        ]
        totals = {row["_id"]: float(row["total"] or 0) async for row in self.deposits.aggregate(pipeline)}
        for f in friends:
            f["total_deposit"] = totals.get(f["user_id"], 0.0)
        friends.sort(key=lambda f: f["total_deposit"], reverse=True)
        return friends

    async def find_ladder_opponent(
        self, exclude_user_id: int, my_rating: float, above_count: int, rating_range: float,
    ) -> Optional[dict]:
        """Соперник по месту в общей Таблице лидеров, а не по редкости орла —
        случайный другой игрок из объединения (а) above_count ближайших мест
        НАД игроком в рейтинге (стимул подниматься выше) и (б) любых игроков
        в пределах ±rating_range очков рейтинга. Так серый орёл может
        встретить синего, если они рядом по рейтингу. Отсутствующий
        pvp_rating (аккаунты до Арены) трактуется как стартовые 1000, как
        и везде в Арене."""
        above_pipeline = [
            {"$addFields": {"_rating": {"$ifNull": ["$pvp_rating", 1000]}}},
            {"$match": {"_id": {"$ne": exclude_user_id}, "_rating": {"$gt": my_rating}}},
            {"$sort": {"_rating": 1}},
            {"$limit": above_count},
            {"$project": {"name": 1, "monsters": 1, "nest_equipped": 1, "pvp_rating": "$_rating"}},
        ]
        nearby_pipeline = [
            {"$addFields": {"_rating": {"$ifNull": ["$pvp_rating", 1000]}}},
            {"$match": {
                "_id": {"$ne": exclude_user_id},
                "_rating": {"$gte": my_rating - rating_range, "$lte": my_rating + rating_range},
            }},
            {"$sample": {"size": 20}},
            {"$project": {"name": 1, "monsters": 1, "nest_equipped": 1, "pvp_rating": "$_rating"}},
        ]
        candidates = {}
        async for doc in self.users.aggregate(above_pipeline):
            candidates[doc["_id"]] = doc
        async for doc in self.users.aggregate(nearby_pipeline):
            candidates[doc["_id"]] = doc
        if not candidates:
            return None
        doc = random.choice(list(candidates.values()))
        return {
            "user_id": doc["_id"], "name": doc.get("name") or "",
            "pvp_rating": doc.get("pvp_rating"),
            "monsters": doc.get("monsters"), "nest_equipped": doc.get("nest_equipped"),
        }

    async def get_leaderboard(self, limit: int = 100) -> list:
        """Топ-N реальных игроков по pvp_rating, по убыванию — общая на всех
        Таблица лидеров Арены. Отсутствующий pvp_rating (аккаунты до Арены)
        трактуется как стартовые 1000, как и в pvp_rating_of на сервере.
        Редкость орла (monsters) сюда намеренно не проецируется — Топ-100
        показывает только место/ник/рейтинг, а награды за призовые места
        (см. distribute_arena_rewards) читают monsters отдельно, только для
        того самого игрока, а не для всей таблицы разом."""
        pipeline = [
            {"$addFields": {"_rating": {"$ifNull": ["$pvp_rating", 1000]}}},
            {"$sort": {"_rating": -1}},
            {"$limit": limit},
            {"$project": {"name": 1, "pvp_rating": "$_rating"}},
        ]
        docs = []
        async for doc in self.users.aggregate(pipeline):
            docs.append({
                "user_id": doc["_id"], "name": doc.get("name") or "",
                "pvp_rating": doc.get("pvp_rating"),
            })
        return docs

    async def count_higher_rating(self, rating: float) -> int:
        """Сколько игроков строго выше данного рейтинга — чтобы посчитать
        место игрока, не попавшего в Топ-100."""
        pipeline = [
            {"$addFields": {"_rating": {"$ifNull": ["$pvp_rating", 1000]}}},
            {"$match": {"_rating": {"$gt": rating}}},
            {"$count": "n"},
        ]
        async for doc in self.users.aggregate(pipeline):
            return doc["n"]
        return 0

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

    async def count_online(self, since_ts: float) -> int:
        return await self.users.count_documents({"last_seen": {"$gte": since_ts}})

    async def stats(self, online_since: Optional[float] = None) -> dict:
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
        online = await self.count_online(online_since) if online_since is not None else 0

        return {
            "players": int(base.get("players", 0)),
            "online": online,
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
    """MongoDB — единственный бэкенд. MONGODB_URI (или MONGO_URL) обязателен,
    чтобы данные никогда молча не уезжали в локальный (эфемерный на хостинге)
    SQLite-файл при неверно настроенном окружении."""
    uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URL")
    if not uri and client is None:
        raise RuntimeError(
            "MONGODB_URI (или MONGO_URL) не задан. Хранилище — только MongoDB, "
            "локального SQLite-фолбэка больше нет."
        )

    db_name = os.getenv("MONGODB_DB", "monstergram")
    print(f"[storage] backend = MongoDB, db = {db_name!r}, uri = {_mask_uri(uri) if uri else '(client passed in)'}")
    return MongoStore(uri, db_name, client=client)
