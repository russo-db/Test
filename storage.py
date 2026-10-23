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
    pvp_rating (Арена: рейтинг для таблицы лидеров, старт 1000, +25/-15 за
    победу/поражение, сбрасывается всем разом раз в сезон — см. arena_season)
    pvp_energy, pvp_energy_day (Арена: энергия на вход в бой, потолок 10,
    пополняется раз в UTC-сутки; day — номер суток последнего пополнения)
    clan_id (Кланы: ObjectId клана, в котором состоит игрок, строкой; None —
    не состоит ни в одном; см. clans ниже)
    burned_power (Кланы: личный, НАВСЕГДА закреплённый за игроком вклад в
    силу клана — очки за сожжённых орлов 7 уровня, см.
    burn_eagle_for_clan_power. Не обнуляется при выходе/исключении из клана
    и продолжает считаться, даже если игрок сейчас ни в одном клане не
    состоит — просто временно ни в чью clan_power не входит)

Кроме игроков хранятся пополнения (deposits, ключ — хэш транзакции TON),
заявки на вывод (withdrawals), лоты рынка (market_listings — P2P-торговля
орлами между игроками), лоты рынка снаряжения (equip_listings — P2P-торговля
предметами Кузницы по грейдам) и рынка ресурсов (resource_listings —
P2P-торговля целыми Небесными Осколками и целыми частичками), общий (один
на всех игроков, не по-пользовательски) счётчик лавки купца —
merchant_state: {meat_bought, eagles_sold_by_tier: {tier: n}}, точно так же общий
кулдаун/суточный лимит покупки Небесного Осколка —
nest_state: {cooldown_until, day, bought_today}, и общий номер текущего
сезона Арены — arena_season: {season, started_at, ends_at, days} (см. advance_arena_season /
reconcile_arena_season в main.py — раз в ARENA_SEASON_DAYS суток Топ-50
получает призы и PvP-рейтинг сбрасывается всем игрокам).

Кланы — clans: {name, leader_id, members: [user_id...], open_slots,
applications: [user_id...], lineup_submissions: {user_id: {tier_id}},
approved_lineup: [{user_id, tier_id}...]} — до 15 участников. clan_power
НЕ хранится в документе клана — это производная величина, сумма личного
burned_power (см. поле игрока выше) всех ТЕКУЩИХ участников, считается на
лету в get_clan/list_top_clans: вышедший игрок сразу перестаёт вносить
вклад в свой бывший клан, принятый — сразу начинает вносить вклад в новый,
без единого места, где эту сумму нужно было бы вручную инкрементить/
декрементить (и, соответственно, без риска рассинхронизации). Сжигание
орла 7 уровня начисляет очки исключительно на аккаунт сжёгшего игрока
(см. burn_eagle_for_clan_power в main.py). Вступление — через заявку
(см. apply_to_clan): игрок без клана
подаёт заявку в любой клан, лидер сам решает, принять (accept_clan_application
— только если есть открытое место) или отклонить (reject_clan_application);
заявка живёт в applications, пока лидер её не разрешит, а все заявки игрока
разом снимаются, как только он создаёт собственный клан (см. create_clan).
Турнир кланов сам ботов/NPC не генерирует: участвуют только кланы, уже
существующие на сервере (в т.ч. тестовые клан-боты, созданные админом —
см. create_bot_clan), и турнир запускается ТОЛЬКО вручную админом
(см. /admin/api/clan_tournament/start в main.py), выбравшим масштаб
8/16/32 (CLAN_TOURNAMENT_SIZES) — берутся ровно первые size сильнейших
кланов по clan_power, состав замораживается на весь турнир —
clan_tournament: {_id: "current", cycle, size, start_at, match_minute, bracket:
[{round, day, clan_a_id, clan_a_name, clan_b_id, clan_b_name, resolved,
winner_id, winner_name, battle_log, resolved_at, fighters_a, fighters_b,
start_time_override?}...]} (см. try_launch_clan_tournament
и reconcile_clan_tournament в main.py — турнир только лениво
ПРОДВИГАЕТСЯ по дням/матчам между запусками, но никогда не запускается
и не перезапускается сам).
"""

import asyncio
import contextvars
import os
import random
import re
import time
from typing import Optional

FIELDS = (
    "user_id", "name", "coins", "total_earned", "mnstr", "gold", "monsters", "farm_queue",
    "active_slot", "missions", "slots", "referrals", "referred_by", "last_seen",
    "daily_day", "daily_last", "daily_cycles", "eggs_board", "eggs_board_unlocked", "eggs_queue", "wallet", "ops",
    "vip_tier", "vip_expires_at", "vip_last_meat_at", "wheel_day", "wheel_spins_today",
    "nest_miners", "nest_particles", "nest_last_claim", "nest_inventory", "nest_equipped", "pvp_rating",
    "pvp_energy", "pvp_energy_day", "clan_id", "burned_power", "created_at",
)

# --- ЖУРНАЛ БАЛАНСОВ ---
# Каждое изменение GRAM (coins), Meat (mnstr) и золота (gold) игрока пишется в
# balance_ledger: {user_id, ts, source, delta: {поле: изменение}, balance: {поле:
# баланс после}}. Все записи балансов идут через users.update_one, поэтому
# журнал ведётся в одном месте — LedgerUsers ниже, — и новое действие в игре
# попадает в журнал само, без правок в его коде. Источник — адрес запроса
# (ставится middleware в main.py) либо явная метка фонового начисления
# (ledger_source("ton:deposit") и т.п.).
LEDGER_FIELDS = ("coins", "mnstr", "gold")
LEDGER_SOURCE = contextvars.ContextVar("ledger_source", default="system")
LEDGER_RETENTION_SECONDS = 90 * 24 * 3600   # записи старше 90 дней удаляются (prune_ledger)
# Операции одного игрока из одного источника за LEDGER_BUCKET_SECONDS
# складываются в ОДНУ запись (сумма изменений + счётчик операций): 100 тапов
# кормления подряд — одна строка журнала, а не сто. Ключ записи — её _id,
# поэтому запись не требует никаких дополнительных индексов в базе.
LEDGER_BUCKET_SECONDS = 600
# Журнал снаряжения (item_log): каждое изменение вещей игрока — стопки +0
# (nest_inventory), заточенные вещи (nest_enchanted), надетое на орлов
# (nest_equipped / nest_equipped_enchant) — пишется сюда же, в LedgerUsers:
# {user_id, ts, source, changes: [{place, type, grade, enchant, delta}]}.
# place: "stack" — стопка +0 на складе, "item" — заточенная вещь на складе,
# "eagle:<редкость>" — надета на орла. Нужен для споров «скрафтил, а вещи нет».
ITEM_FIELDS = ("nest_inventory", "nest_enchanted", "nest_equipped", "nest_equipped_enchant")
ITEM_LOG_RETENTION_SECONDS = 90 * 24 * 3600


def _touches_items(update: dict) -> bool:
    for op in ("$set", "$inc", "$push", "$pull", "$unset", "$addToSet"):
        for key in (update.get(op) or {}):
            if key.split(".", 1)[0] in ITEM_FIELDS:
                return True
    return False


def item_bag(doc: Optional[dict]) -> dict:
    """{(place, type, grade, enchant): количество} — все вещи игрока."""
    bag = {}

    def add(key, n=1):
        bag[key] = bag.get(key, 0) + n

    doc = doc or {}
    inv = doc.get("nest_inventory")
    if isinstance(inv, dict):
        for t, sub in inv.items():
            if isinstance(sub, dict):
                for g, n in sub.items():
                    try:
                        n = int(n or 0)
                    except (TypeError, ValueError):
                        continue
                    if n > 0:
                        add(("stack", str(t), str(g), 0), n)
    for it in doc.get("nest_enchanted") or []:
        if isinstance(it, dict):
            try:
                add(("item", str(it.get("type")), str(it.get("grade")), int(it.get("enchant") or 0)))
            except (TypeError, ValueError):
                pass
    eq = doc.get("nest_equipped")
    lv = doc.get("nest_equipped_enchant") if isinstance(doc.get("nest_equipped_enchant"), dict) else {}
    if isinstance(eq, dict):
        for tier, sub in eq.items():
            if isinstance(sub, dict):
                for t, g in sub.items():
                    if g:
                        try:
                            lvl = int(((lv.get(tier) or {}) if isinstance(lv.get(tier), dict) else {}).get(t) or 0)
                        except (TypeError, ValueError):
                            lvl = 0
                        add((f"eagle:{tier}", str(t), str(g), lvl))
    return bag


def apply_item_update(before: dict, update: dict) -> Optional[dict]:
    """Вещи игрока ПОСЛЕ update, посчитанные из атомарно прочитанного «до» —
    без второго чтения, в которое могла бы попасть чужая параллельная запись.
    None — форма update незнакома, тогда вызывающий перечитывает документ."""
    import copy
    doc = {f: copy.deepcopy(before.get(f)) for f in ITEM_FIELDS if f in before}
    for op, body in update.items():
        for path, val in (body or {}).items():
            parts = path.split(".")
            if parts[0] not in ITEM_FIELDS:
                continue
            if op in ("$set", "$unset", "$inc"):
                node = doc
                for key in parts[:-1]:
                    if not isinstance(node.get(key), dict):
                        if op == "$unset":
                            node = None
                            break
                        node[key] = {}
                    node = node[key]
                if node is None:
                    continue
                last = parts[-1]
                if op == "$set":
                    node[last] = copy.deepcopy(val)
                elif op == "$unset":
                    node.pop(last, None)
                else:
                    try:
                        node[last] = (node.get(last) or 0) + val
                    except TypeError:
                        return None
            elif op == "$push" and len(parts) == 1:
                each = isinstance(val, dict) and "$each" in val
                if each and set(val) - {"$each"}:   # $slice/$sort/$position — не считаем сами
                    return None
                items = val["$each"] if each else [val]
                doc[parts[0]] = list(doc.get(parts[0]) or []) + copy.deepcopy(list(items))
            elif op == "$pull" and len(parts) == 1 and isinstance(val, dict) \
                    and not any(str(k).startswith("$") for k in val):
                doc[parts[0]] = [it for it in (doc.get(parts[0]) or [])
                                 if not (isinstance(it, dict) and all(it.get(k) == v for k, v in val.items()))]
            else:
                return None
    return doc


def item_changes(before: Optional[dict], after: Optional[dict]) -> list:
    a, b = item_bag(before), item_bag(after)
    out = []
    for key in sorted(set(a) | set(b)):
        d = b.get(key, 0) - a.get(key, 0)
        if d:
            out.append({"place": key[0], "type": key[1], "grade": key[2], "enchant": key[3], "delta": d})
    return out
EGG_LOG_RETENTION_SECONDS = 90 * 24 * 3600   # журнал вскрытия яиц — тоже 90 дней


def resource_lot_price(listing: dict) -> tuple:
    """(поле баланса, цена) лота рынка ресурсов: лоты Частичек за золото —
    ("gold", price_gold), остальные — ("coins", price_gram)."""
    if listing.get("currency") == "gold":
        return "gold", float(listing.get("price_gold") or 0)
    return "coins", float(listing.get("price_gram") or 0)


class ledger_source:
    """with ledger_source("ton:deposit"): ... — метка источника для журнала."""

    def __init__(self, source: str):
        self.source = source

    def __enter__(self):
        self._token = LEDGER_SOURCE.set(self.source)
        return self

    def __exit__(self, *exc):
        LEDGER_SOURCE.reset(self._token)
        return False


class _LedgerUpdateResult:
    """Совместим с pymongo UpdateResult в той части, что использует код."""

    def __init__(self, matched: int):
        self.acknowledged = True
        self.matched_count = matched
        self.modified_count = matched
        self.upserted_id = None
        self.raw_result = {"n": matched}


class LedgerUsers:
    """Коллекция игроков, которая журналирует изменения балансов. update_one,
    задевающий coins/mnstr/gold, выполняется как find_one_and_update с
    возвратом документа ДО изменения — одна атомарная операция, поэтому
    разница «было → стало» точная даже при параллельных запросах. Остальные
    методы — без изменений (проксируются в настоящую коллекцию)."""

    def __init__(self, collection, ledger, item_log=None):
        self._collection = collection
        self._ledger = ledger
        self._item_log = item_log

    def __getattr__(self, name):
        return getattr(self._collection, name)

    async def update_one(self, filter, update, upsert=False, **kwargs):
        if not isinstance(update, dict):   # update-pipeline — балансы так не пишем
            return await self._collection.update_one(filter, update, upsert=upsert, **kwargs)
        inc = update.get("$inc") or {}
        set_ = update.get("$set") or {}
        touched = [f for f in LEDGER_FIELDS if f in inc or f in set_]
        items = self._item_log is not None and not upsert and _touches_items(update)
        if upsert or not (touched or items):
            result = await self._collection.update_one(filter, update, upsert=upsert, **kwargs)
            if upsert and result.upserted_id is not None:
                start = {f: float(v) for f, v in ((update.get("$setOnInsert") or {}).items())
                         if f in LEDGER_FIELDS and v}
                if start:
                    await self._record(result.upserted_id, start, start, source="start")
            return result
        from pymongo import ReturnDocument
        projection = {f: 1 for f in LEDGER_FIELDS + (ITEM_FIELDS if items else ())}
        before = await self._collection.find_one_and_update(
            filter, update, projection=projection,
            return_document=ReturnDocument.BEFORE, **kwargs,
        )
        if before is None:
            return _LedgerUpdateResult(0)
        if items:
            await self._record_items(before, update)
        delta, balance = {}, {}
        for f in touched:
            old = float(before.get(f) or 0)
            new = old + float(inc[f] or 0) if f in inc else float(set_[f] or 0)
            if abs(new - old) > 1e-9:
                delta[f] = round(new - old, 9)
                balance[f] = round(new, 9)
        if delta:
            await self._record(before["_id"], delta, balance)
        return _LedgerUpdateResult(1)

    async def find_one_and_update(self, filter, update, *args, **kwargs):
        """Как у коллекции; если меняются вещи — ещё и запись в журнал снаряжения.
        «До» берётся из самой операции (BEFORE) — атомарно, без отдельного чтения."""
        from pymongo import ReturnDocument
        if (self._item_log is None or args or not isinstance(update, dict) or not _touches_items(update)
                or kwargs.get("upsert") or kwargs.get("return_document", ReturnDocument.BEFORE) != ReturnDocument.BEFORE):
            return await self._collection.find_one_and_update(filter, update, *args, **kwargs)
        projection = kwargs.get("projection")
        if isinstance(projection, dict) and projection and all(v in (1, True) for v in projection.values()):
            extra = [f for f in ITEM_FIELDS if f not in projection]
            kwargs["projection"] = {**projection, **{f: 1 for f in extra}}
        elif projection is not None:   # исключающая/непривычная проекция — без журнала
            return await self._collection.find_one_and_update(filter, update, *args, **kwargs)
        else:
            extra = []
        before = await self._collection.find_one_and_update(filter, update, **kwargs)
        if before is None:
            return None
        await self._record_items(before, update)
        for f in extra:   # вызывающему — ровно те поля, что он просил
            before.pop(f, None)
        return before

    async def _record_items(self, before: dict, update: dict) -> None:
        """Сравнивает вещи до записи и после неё. Сбой журнала не ломает действие."""
        try:
            after = apply_item_update(before, update)
            if after is None:
                after = await self._collection.find_one({"_id": before["_id"]}, {f: 1 for f in ITEM_FIELDS})
            changes = item_changes(before, after)
            if changes:
                await self._item_log.insert_one({"user_id": before["_id"], "ts": time.time(),
                                                 "source": LEDGER_SOURCE.get(), "changes": changes})
        except Exception as e:
            print(f"[item_log] write failed for {before.get('_id')}: {type(e).__name__}: {e}")

    async def _record(self, user_id, delta: dict, balance: dict, source: Optional[str] = None) -> None:
        """Сбой записи журнала никогда не ломает само действие игрока."""
        try:
            now = time.time()
            source = source or LEDGER_SOURCE.get()
            bucket = int(now // LEDGER_BUCKET_SECONDS)
            update = {
                "$inc": {"count": 1, **{f"delta.{f}": v for f, v in delta.items()}},
                "$max": {"ts": now},
                "$setOnInsert": {"user_id": user_id, "source": source, "first_ts": now},
            }
            if balance:
                update["$set"] = {f"balance.{f}": v for f, v in balance.items()}
            await self._ledger.update_one({"_id": f"{user_id}:{bucket}:{source}"}, update, upsert=True)
        except Exception as e:
            print(f"[ledger] write failed for {user_id}: {type(e).__name__}: {e}")


class MongoStore:
    """MongoDB через motor. Документ хранит списки как есть, без JSON-строк."""

    def __init__(self, uri: str, db_name: str, client=None):
        if client is None:
            from motor.motor_asyncio import AsyncIOMotorClient

            client = AsyncIOMotorClient(uri)
        # Журнал балансов (см. LedgerUsers) — пишется при каждом изменении coins/mnstr/gold.
        self.balance_ledger = client[db_name]["balance_ledger"]
        # Журнал снаряжения: каждое изменение вещей игрока (см. ITEM_FIELDS, LedgerUsers).
        self.item_log = client[db_name]["item_log"]
        self.users = LedgerUsers(client[db_name]["users"], self.balance_ledger, self.item_log)
        self.deposits = client[db_name]["deposits"]
        self.withdrawals = client[db_name]["withdrawals"]
        self.market = client[db_name]["market_listings"]
        self.equip_market = client[db_name]["equip_listings"]
        self.resource_market = client[db_name]["resource_listings"]
        # лоты, которые миграция не смогла разобрать (битое количество/продавец) — не удаляются, а ждут разбора
        self.resource_quarantine = client[db_name]["resource_listings_quarantine"]
        self.alchemy_market = client[db_name]["alchemy_listings"]
        self.merchant = client[db_name]["merchant_state"]
        self.nest_global = client[db_name]["nest_state"]
        self.arena_season = client[db_name]["arena_season"]
        self.clans = client[db_name]["clans"]
        self.clan_tournament = client[db_name]["clan_tournament"]
        self.counters = client[db_name]["counters"]
        # Статистика потоков экономики для админки: _id "total" — за всё
        # время, "day:YYYY-MM-DD" — за сутки UTC (см. record_economy).
        self.economy = client[db_name]["economy_stats"]
        # Настройки сервера, которые админ меняет на лету и которые должны
        # пережить перезапуск/деплой (в отличие от game_config.json): {_id: ключ, value}.
        self.settings = client[db_name]["settings"]
        # NFT «Небесный орел»: {_id: сырой адрес NFT, last_claim, user_id} —
        # таймер самой NFT (см. /api/nft/claim-shard в main.py).
        self.nft_claims = client[db_name]["nft_claims"]
        # Аукцион «Раздача Небесных орлов»: {_id, title, item_image, min_bid,
        # step, ends_at, status active|finished|cancelled, version, top: [{user_id,
        # name, bid, ts}] (до 5, место 1 — первым), pending_refunds, settled,
        # winners}. Замороженная ставка игрока лежит в его документе —
        # auction_holds.<id аукциона> (см. place_auction_bid).
        self.auctions = client[db_name]["auctions"]
        # Журнал вскрытия яиц (для разбора споров в админке): {user_id, ts,
        # level, kind eagle|meat|jackpot, amount, monster, placed farm|queue,
        # source open|open_all}. Хранится EGG_LOG_RETENTION_SECONDS.
        self.egg_log = client[db_name]["egg_log"]
        # Посещения по дням для Retention: {_id: "user_id:YYYY-MM-DD", user_id,
        # date} — одна строка на игрока в сутки (сутки по UTC, как и в игре).
        self.user_activity = client[db_name]["user_activity"]
        # Журнал наград ежедневного входа: {user_id, ts, date, day, first_lap,
        # gram, mnstr, monster, placed farm|queue|None} — одна запись на выдачу.
        self.daily_claims = client[db_name]["daily_claims"]
        # Журнал сделок рынка: кто у кого что купил и за сколько (все 3 отдела).
        self.market_trades = client[db_name]["market_trades"]
        # ⚔️ Лобби рейдов на Мировых боссов (см. раздел МИРОВЫЕ БОССЫ в main.py):
        # {_id: 10 hex, boss_id, status gathering|battle|won|lost|disbanded,
        #  leader_id, leader_name, created_at, expires_at (сбор 30 мин),
        #  battle_started_at, battle_ends_at (бой 10 мин), last_summon_at,
        #  members: [{user_id, name, monster_id, tier_id, payment, amount,
        #  paid, joined_at}] (до 5), result {damage, hp, won, gold, by_member},
        #  reason (почему распущено), finished_at}. Залог игрока лежит в его
        # документе — raid_entry {lobby, payment, amount, monster_id, at}.
        self.raids = client[db_name]["raid_lobbies"]
        # 🏔 Орлиная Скала (см. раздел ОРЛИНАЯ СКАЛА в main.py): _id "state" —
        # одно общее состояние (активность, владелец, лидеры фракций, вызовы,
        # гарнизон, текущая осада, версия v для CAS-записи).
        self.rock = client[db_name]["eagle_rock"]
        # Лента системных сообщений фракций: {faction, ts, text, text_en}.
        self.rock_feed = client[db_name]["rock_feed"]
        # Архив осад/вызовов с выплатами Очков Заслуг: {_id, kind, payouts {uid: n},
        # paid (выплаты разнесены), ...}. Выплата игроку идемпотентна (rock_paid).
        self.rock_history = client[db_name]["rock_history"]
        # 📣 Великий призыв Лидера: очередь рассылки {_id, faction, queue [uid по
        # убыванию силы], next (сколько обработано), status pending|done,
        # lease_until (кто сейчас шлёт), sent, failed, created_at}.
        self.rock_calls = client[db_name]["rock_calls"]
        # 🛡️ Готовность к бою на сутки: _id "{day}:{user_id}:{type}", поля
        # faction_id, user_id, day, type pre_confirmed (заранее, для подтягивания
        # при призыве) | live_assault (сам вошёл в штурм/резерв), eagles_count,
        # total_power, has_contributed, pulled_sieges [id осад, куда подтянут], updated_at.
        self.rock_ready = client[db_name]["faction_daily_readiness"]

    async def init(self):
        # Индексы — ускорение, а не условие работы. Построить новый индекс
        # MongoDB соглашается только при >= 500 МБ свободного диска
        # (indexBuildMinAvailableDiskSpaceMB); на маленьком томе это
        # OutOfDiskSpace. Такая ошибка не должна останавливать запуск игры.
        for collection, keys, kwargs in (
            (self.users, "referred_by", {}),
            (self.deposits, "user_id", {}),
            (self.withdrawals, "user_id", {}),
            (self.market, "seller_id", {}),
            (self.equip_market, "seller_id", {}),
            (self.resource_market, "seller_id", {}),
            (self.market, [("monster_id", 1), ("price_gram", 1)], {}),
            (self.equip_market, [("grade", 1), ("price_gram", 1)], {}),
            (self.resource_market, [("resource", 1), ("price_gram", 1)], {}),
            (self.resource_market, [("resource", 1), ("unit_price", 1)], {}),
            (self.alchemy_market, "seller_id", {}),
            (self.alchemy_market, [("item", 1), ("created_at", 1)], {}),
            (self.egg_log, [("user_id", 1), ("ts", -1)], {}),
            (self.user_activity, "date", {}),
            (self.daily_claims, [("ts", -1)], {}),
            (self.daily_claims, "day", {}),
            (self.market_trades, [("ts", -1)], {}),
            (self.market_trades, "buyer_id", {}),
            (self.market_trades, "seller_id", {}),
            (self.balance_ledger, [("source", 1), ("ts", -1)], {}),
            (self.balance_ledger, [("ts", -1)], {}),
            (self.item_log, [("user_id", 1), ("ts", -1)], {}),
            (self.users, "auto_expedition_until", {"sparse": True}),
            (self.users, "raid_entry.lobby", {"sparse": True}),
            (self.raids, [("status", 1), ("expires_at", 1)], {}),
            (self.raids, [("status", 1), ("battle_ends_at", 1)], {}),
            (self.raids, [("status", 1), ("created_at", -1)], {}),
            (self.users, "faction_id", {"sparse": True}),
            # «онлайн/неактивные»: счётчики админки и Арены, исключение неактивных из фракций
            (self.users, "last_seen", {}),
            (self.rock_feed, [("faction", 1), ("ts", -1)], {}),
            (self.rock_history, [("paid", 1), ("ts", 1)], {}),
            (self.rock_history, [("ts", -1)], {}),
            (self.rock_calls, [("status", 1), ("created_at", 1)], {}),
            (self.rock_ready, [("faction_id", 1), ("day", 1)], {}),
            (self.rock_ready, [("user_id", 1), ("day", 1)], {}),
        ):
            try:
                await collection.create_index(keys, **kwargs)
            except Exception as e:
                print(f"[storage] index {collection.name}.{keys} not created: {type(e).__name__}: {e}")
        # Лоты ресурсов, выставленные до появления unit_price, — досчитываем.
        try:
            async for doc in self.resource_market.find({"unit_price": {"$exists": False}}, {"price_gram": 1, "amount": 1}).limit(20000):
                unit = float(doc.get("price_gram") or 0) / max(1, int(doc.get("amount") or 1))
                await self.resource_market.update_one({"_id": doc["_id"]}, {"$set": {"unit_price": unit}})
        except Exception as e:
            print(f"[storage] unit_price backfill failed: {type(e).__name__}: {e}")
        # Купец — общая на всех игроков лавка с разовыми лимитами; документ один
        # (_id = "global"), никак не привязан к конкретному user_id.
        await self.merchant.update_one(
            {"_id": "global"}, {"$setOnInsert": {"meat_bought": 0, "eagles_sold_by_tier": {}}}, upsert=True
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


    async def get_merchant_state(self) -> dict:
        """meat_bought — просто статистика (обмен золота на Meat без лимита);
        eagles_sold_by_tier — сколько орлов каждой редкости купец уже выкупил
        (лимиты общие на всех игроков, см. sell_merchant_eagle)."""
        doc = await self.merchant.find_one({"_id": "global"}) or {}
        by_tier = {k: int(v or 0) for k, v in (doc.get("eagles_sold_by_tier") or {}).items()}
        return {
            "meat_bought": float(doc.get("meat_bought") or 0),
            "eagles_sold_by_tier": by_tier, "eagles_sold": sum(by_tier.values()),
        }

    async def reset_merchant_state(self) -> dict:
        await self.merchant.update_one(
            {"_id": "global"}, {"$set": {"meat_bought": 0, "eagles_sold_by_tier": {}}, "$unset": {"eagles_sold": ""}},
            upsert=True,
        )
        return {"meat_bought": 0.0, "eagles_sold_by_tier": {}, "eagles_sold": 0}

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
                              cooldown_seconds: float, daily_limit: int, new_last_claim: float,
                              expected_ops: Optional[int] = None) -> dict:
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
        # new_last_claim посчитан по документу с ops == expected_ops: если игрок
        # с тех пор что-то сделал (например, собрал частички), запись не
        # пройдёт — иначе старая точка отсчёта начислила бы частички повторно.
        charge_filter = {"_id": user_id, "coins": {"$gte": price_gram}}
        if expected_ops is not None:
            charge_filter["ops"] = int(expected_ops)
        charge = await self.users.update_one(
            charge_filter,
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
            user = await self.users.find_one({"_id": user_id}, {"coins": 1}) or {}
            if float(user.get("coins") or 0) < price_gram:
                return {"status": "insufficient_gram"}
            return {"status": "stale"}   # документ игрока изменился — вызывающий перечитает и повторит

        return {"status": "ok", "cooldown_until": new_cooldown, "bought_today": bought_today + 1}

    async def buy_merchant_meat(self, user_id: int, amount: float, rate: float) -> dict:
        """Обмен золота на Meat по фиксированному курсу — постоянный, без
        лимита количества: единственное условие — хватает ли золота, и оно
        проверяется атомарно в том же update, что и списание. meat_bought
        в общей лавке — только статистика для админки."""
        cost = amount / rate
        charge = await self.users.update_one(
            {"_id": user_id, "gold": {"$gte": cost}},
            {"$inc": {"gold": -cost, "mnstr": amount, "ops": 1}},
        )
        if charge.modified_count == 0:
            return {"status": "insufficient_gold"}
        await self.merchant.update_one({"_id": "global"}, {"$inc": {"meat_bought": amount}}, upsert=True)
        return {"status": "ok", "amount": amount, "cost": cost}

    async def sell_merchant_eagle(self, user_id: int, slot_index: int, buyback: dict,
                                   monster_tier: dict, feed_levels: int,
                                   expected_monster_id: Optional[str] = None) -> dict:
        """Выкуп орла купцом: цена и лимит — по редкости (buyback: {tier:
        {"price", "limit"}}); лимит общий на всех игроков. Тот же двухфазный
        подход: сначала атомарно резервируем место в лимите редкости, потом
        меняем ферму продавца по оптимистичной блокировке (полное совпадение
        monsters И farm_queue) и откатываем резерв при конфликте. Купец берёт
        только полностью откормленных (feed_level >= feed_levels) орлов.
        Освободившийся слот сразу добирает орла из очереди (farm_queue)."""
        doc = await self.users.find_one(
            {"_id": user_id}, {"monsters": 1, "active_slot": 1, "farm_queue": 1, "slots": 1}
        )
        farm = list((doc or {}).get("monsters") or [])
        active_slot = int((doc or {}).get("active_slot") or 0)
        original_queue = list((doc or {}).get("farm_queue") or [])
        slots_count = int((doc or {}).get("slots") or 0)
        if not (0 <= slot_index < len(farm)):
            return {"status": "not_found"}
        if expected_monster_id is not None and farm[slot_index].get("id") != expected_monster_id:
            return {"status": "stale"}   # ферма сдвинулась — в ячейке уже другой орёл
        tier = monster_tier.get(farm[slot_index].get("id"))
        offer = buyback.get(tier)
        if not offer:
            return {"status": "wrong_tier"}
        price, limit = float(offer["price"]), int(offer["limit"])
        if farm[slot_index].get("listing_id"):
            return {"status": "listed"}
        if int(farm[slot_index].get("expedition_until") or 0) > 0:
            return {"status": "on_expedition"}
        if int(farm[slot_index].get("hunt_until") or 0) > 0:
            return {"status": "on_hunt"}   # Дальняя Экспедиция (Охота)
        if farm[slot_index].get("raid_id"):
            return {"status": "in_raid"}   # ⚔️ лобби/бой Мирового босса
        if int(farm[slot_index].get("feed_level") or 0) < feed_levels:
            return {"status": "not_fed"}
        if len(farm) <= 1:
            return {"status": "last_eagle"}

        field = f"eagles_sold_by_tier.{tier}"
        # $not/$gte, а не $lte: поле редкости может ещё отсутствовать.
        reserve = await self.merchant.update_one(
            {"_id": "global", field: {"$not": {"$gte": limit}}},
            {"$inc": {field: 1}},
        )
        if reserve.modified_count == 0:
            return {"status": "limit_reached", "tier": tier}

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
            await self.merchant.update_one({"_id": "global"}, {"$inc": {field: -1}})
            return {"status": "conflict"}
        return {"status": "ok", "tier": tier, "price": price}

    # --- РЫНОК ОРЛОВ. Выставленный орёл НЕ снимается с фермы: он остаётся в
    # своей ячейке с отметкой listing_id (замок) и не может ничего делать,
    # пока лот не куплен (тогда ячейка уходит из фермы продавца) или не снят
    # (отметка просто убирается). Каждая правка monsters/farm_queue здесь
    # увеличивает ops — иначе параллельное действие фермы (run_farm_action,
    # CAS по ops) могло бы записать поверх устаревший массив и «воскресить»
    # проданного орла. Лоты, созданные до этой схемы (орёл тогда снимался
    # с фермы), не имеют in_place и обрабатываются по-старому. ---

    async def create_listing(self, seller_id: int, seller_name: str, monster_id: str,
                              feed_levels: int, price_gram: float, ts: int,
                              slot_index: Optional[int] = None) -> tuple:
        """Запирает на ферме продавца полностью прокачанного орла нужного вида
        (конкретную ячейку slot_index или первую подходящую) и создаёт лот.
        Возвращает (listing_id, None) или (None, причина): "not_found",
        "listed" (уже выставлен), "on_expedition", "conflict"."""
        from bson import ObjectId

        for _ in range(5):
            doc = await self.users.find_one({"_id": seller_id}, {"monsters": 1, "ops": 1})
            farm = list((doc or {}).get("monsters") or [])
            ops = int((doc or {}).get("ops") or 0)

            def fits(m):
                return m.get("id") == monster_id and int(m.get("feed_level") or 0) >= feed_levels

            if slot_index is not None:
                if not (0 <= slot_index < len(farm)) or not fits(farm[slot_index]):
                    return None, "not_found"
                idx = slot_index
            else:
                idx = next((i for i, m in enumerate(farm)
                            if fits(m) and not m.get("listing_id") and not int(m.get("expedition_until") or 0)
                            and not int(m.get("hunt_until") or 0) and not m.get("raid_id")), None)
                if idx is None:
                    return None, "not_found"
            if farm[idx].get("listing_id"):
                return None, "listed"
            if int(farm[idx].get("expedition_until") or 0) > 0:
                return None, "on_expedition"
            if int(farm[idx].get("hunt_until") or 0) > 0:
                return None, "on_hunt"   # Дальняя Экспедиция (Охота)
            if farm[idx].get("raid_id"):
                return None, "in_raid"   # ⚔️ лобби/бой Мирового босса

            oid = ObjectId()
            farm[idx] = dict(farm[idx], listing_id=str(oid))
            locked = await self.users.update_one(
                {"_id": seller_id, "ops": ops}, {"$set": {"monsters": farm, "ops": ops + 1}},
            )
            if locked.modified_count == 0:
                continue  # ферма поменялась параллельно — перечитываем
            try:
                await self.market.insert_one({
                    "_id": oid, "seller_id": seller_id, "seller_name": seller_name,
                    "monster_id": monster_id, "price_gram": price_gram, "created_at": ts, "in_place": True,
                })
            except Exception:
                # лот не записался — замок снимаем, иначе орёл навсегда заперт
                # без лота, и «Снять с рынка» отвечало бы «Лот уже продан»
                await self.users.update_one(
                    {"_id": seller_id, "monsters.listing_id": str(oid)},
                    {"$unset": {"monsters.$.listing_id": ""}, "$inc": {"ops": 1}},
                )
                raise
            return str(oid), None
        return None, "conflict"

    # Витрина рынка для конкретного игрока. Раньше отдавались 200 самых
    # НОВЫХ лотов общим списком: при >200 лотах старые пропадали — их нельзя
    # было купить, а продавец не видел их в «Твоих лотах» и не мог снять
    # (орёл так и стоял бы на ферме под замком). Теперь: ВСЕ свои лоты
    # игрока + по каждой вкладке (редкость/грейд/ресурс) per_group самых
    # ДЕШЁВЫХ чужих — именно их вкладка и показывает (сортировка по цене) —
    # и сколько всего чужих лотов во вкладке.
    OWN_LISTINGS_MAX = 1000

    async def listings_view(self, collection, viewer_id: int, groups: dict, per_group: int = 100,
                            sort_field: str = "price_gram") -> tuple:
        items, counts, seen = [], {}, set()

        def add(doc):
            key = str(doc["_id"])
            if key in seen:
                return
            seen.add(key)
            doc["id"] = str(doc.pop("_id"))
            items.append(doc)

        async for doc in collection.find({"seller_id": viewer_id}).sort("created_at", -1).limit(self.OWN_LISTINGS_MAX):
            add(doc)
        for key, flt in groups.items():
            q = dict(flt)
            q["seller_id"] = {"$ne": viewer_id}
            counts[key] = await collection.count_documents(q)
            async for doc in collection.find(q).sort([(sort_field, 1), ("price_gram", 1), ("created_at", 1)]).limit(per_group):
                add(doc)
        return items, counts

    async def list_listings(self, limit: int = 200) -> list:
        cursor = self.market.find().sort("created_at", -1).limit(limit)
        items = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            items.append(doc)
        return items

    async def _place_eagle(self, user_id: int, slot: dict, queue_max: int,
                           charge: float = 0.0) -> str:
        """Кладёт орла в свободную ОТКРЫТУЮ ячейку фермы (monsters короче
        slots), а если все открытые заняты — в неактивные ячейки (farm_queue).
        Новые ячейки при этом не открываются. charge > 0 — заодно атомарно
        списывает GRAM (покупка). "ok" / "insufficient_funds" / "no_room" /
        "conflict"."""
        for _ in range(5):
            doc = await self.users.find_one(
                {"_id": user_id}, {"coins": 1, "slots": 1, "monsters": 1, "farm_queue": 1, "ops": 1},
            )
            if not doc:
                return "not_found"
            if charge and float(doc.get("coins") or 0) < charge:
                return "insufficient_funds"
            farm = list(doc.get("monsters") or [])
            queue = list(doc.get("farm_queue") or [])
            ops = int(doc.get("ops") or 0)
            if len(farm) < int(doc.get("slots") or 0):
                farm.append(slot)
            elif len(queue) < queue_max:
                queue.append(slot)
            else:
                return "no_room"
            flt = {"_id": user_id, "ops": ops}
            update = {"$set": {"monsters": farm, "farm_queue": queue, "ops": ops + 1}}
            if charge:
                flt["coins"] = {"$gte": charge}
                update["$inc"] = {"coins": -charge}
            result = await self.users.update_one(flt, update)
            if result.modified_count:
                return "ok"
        return "conflict"

    async def _refill_from_queue(self, user_id: int) -> None:
        """После ухода орла из фермы освободившуюся открытую ячейку занимает
        первый орёл из неактивных (как при удалении/продаже купцу). Best
        effort: при гонке просто сделает это следующее действие."""
        for _ in range(3):
            doc = await self.users.find_one(
                {"_id": user_id}, {"slots": 1, "monsters": 1, "farm_queue": 1, "active_slot": 1, "ops": 1},
            )
            if not doc:
                return
            farm = list(doc.get("monsters") or [])
            queue = list(doc.get("farm_queue") or [])
            slots = int(doc.get("slots") or 0)
            ops = int(doc.get("ops") or 0)
            active = min(int(doc.get("active_slot") or 0), max(0, len(farm) - 1))
            if not (len(farm) < slots and queue) and active == int(doc.get("active_slot") or 0):
                return
            while len(farm) < slots and queue:
                farm.append(queue.pop(0))
            result = await self.users.update_one(
                {"_id": user_id, "ops": ops},
                {"$set": {"monsters": farm, "farm_queue": queue, "active_slot": active, "ops": ops + 1}},
            )
            if result.modified_count:
                return

    async def _buy_precheck(self, collection, oid, buyer_id: int, queue_max: Optional[int] = None) -> Optional[str]:
        """Отказы, которые видны и без захвата лота: свой лот, не хватает
        GRAM, нет места для орла. Без этой проверки лот на время отказа
        удалялся и вставлялся обратно — в это окно продавец при «Снять»
        получал «Лот уже продан», а покупатель с деньгами — «продан», хотя
        лот на месте (достаточно было жать «Купить» без GRAM). Атомарные
        проверки при самой покупке остаются — это только фильтр."""
        lot = await collection.find_one({"_id": oid}, {"seller_id": 1, "price_gram": 1})
        if not lot:
            return "not_found"
        if int(lot["seller_id"]) == int(buyer_id):
            return "own_listing"
        doc = await self.users.find_one({"_id": buyer_id}, {"coins": 1, "slots": 1, "monsters": 1, "farm_queue": 1})
        if not doc or float(doc.get("coins") or 0) < float(lot.get("price_gram") or 0):
            return "insufficient_funds"
        if queue_max is not None and len(doc.get("monsters") or []) >= int(doc.get("slots") or 0) \
                and len(doc.get("farm_queue") or []) >= queue_max:
            return "no_room"
        return None

    async def buy_listing(self, buyer_id: int, listing_id: str, feed_levels: int,
                           commission: float, queue_max: int) -> str:
        """Покупка лота: лот атомарно забирается (find_one_and_delete — второй
        покупатель его уже не найдёт), с покупателя списываются GRAM и орёл
        кладётся в его свободную открытую ячейку либо в неактивные; продавцу
        зачисляется цена за вычетом комиссии, а запертая ячейка с этим лотом
        уходит из его фермы. При любом отказе лот возвращается на место."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"

        refused = await self._buy_precheck(self.market, oid, buyer_id, queue_max)
        if refused:
            return refused
        listing = await self.market.find_one_and_delete({"_id": oid})
        if not listing:
            return "not_found"
        if int(listing["seller_id"]) == int(buyer_id):
            await self.market.insert_one(listing)
            return "own_listing"

        # Лот «на месте»: запертый орёл должен быть у продавца. Если его нет
        # (например, админ удалил его из фермы), лот висячий — продать его
        # значило бы выдать покупателю орла из воздуха, а продавцу ещё и
        # деньги. Такой лот просто убираем с рынка.
        if listing.get("in_place"):
            holder = await self.users.find_one({"_id": listing["seller_id"], "monsters.listing_id": str(oid)}, {"_id": 1})
            if not holder:
                print(f"[market] висячий лот {oid}: орла у продавца {listing['seller_id']} нет — лот снят")
                return "not_found"

        price = float(listing["price_gram"])
        slot = {"id": listing["monster_id"], "next_egg_at": 0, "feed_level": feed_levels,
                "feed_taps": 0, "expedition_until": 0}
        placed = await self._place_eagle(buyer_id, slot, queue_max, charge=price)
        if placed != "ok":
            await self.market.insert_one(listing)
            return placed

        seller_id = listing["seller_id"]
        seller_credit = price * (1 - commission)
        await self.users.update_one(
            {"_id": seller_id},
            {
                "$pull": {"monsters": {"listing_id": str(oid)}},
                "$inc": {"coins": seller_credit, "total_earned": seller_credit, "ops": 1},
            },
        )
        await self._refill_from_queue(seller_id)
        return "ok"

    async def cancel_listing(self, seller_id: int, listing_id: str,
                              feed_levels: int, queue_max: int) -> str:
        """Снимает лот: у нового лота с ячейки орла просто убирается замок;
        у старого (созданного, когда орёл снимался с фермы) орёл
        возвращается в свободную открытую ячейку или в неактивные."""
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

        if deleted.get("in_place"):
            unlocked = await self.users.update_one(
                {"_id": seller_id, "monsters.listing_id": str(oid)},
                {"$unset": {"monsters.$.listing_id": ""}, "$inc": {"ops": 1}},
            )
            # Орёл «на месте» снят с замка — или его уже нет на ферме (висячий
            # лот): тогда лот просто убран, нового орла взамен не выдаём.
            return "ok"
        slot = {"id": deleted["monster_id"], "next_egg_at": 0, "feed_level": feed_levels,
                "feed_taps": 0, "expedition_until": 0}
        placed = await self._place_eagle(seller_id, slot, queue_max)
        if placed != "ok":
            await self.market.insert_one(deleted)
            return placed
        return "ok"

    # --- РЫНОК СНАРЯЖЕНИЯ: P2P-торговля предметами Кузницы по грейдам.
    # Полностью самодостаточен внутри storage.py (в отличие от рынка
    # ресурсов ниже) — nest_inventory это просто счётчики по (тип, грейд),
    # без побочных игровых формул вроде ставки накопления частичек. ---

    async def _give_equip_item(self, user_id: int, listing: dict) -> None:
        """Вещь из лота — игроку: +0 в счётчик nest_inventory, заточенная —
        отдельной вещью в nest_enchanted со своим уровнем. ops: 1 — иначе
        параллельный крафт/заточка (CAS по ops с $set всего инвентаря)
        затёрли бы выданную вещь."""
        level = int(listing.get("enchant") or 0)
        if level > 0:
            import secrets
            item = {"id": secrets.token_hex(6), "type": listing["item_type"], "grade": listing["grade"], "enchant": level}
            await self.users.update_one({"_id": user_id}, {"$push": {"nest_enchanted": item}, "$inc": {"ops": 1}})
        else:
            field = f"nest_inventory.{listing['item_type']}.{listing['grade']}"
            await self.users.update_one({"_id": user_id}, {"$inc": {field: 1, "ops": 1}})

    async def create_enchanted_equip_listing(self, seller_id: int, seller_name: str, item_id: str,
                                             price_gram: float, ts: int):
        """Выставляет ЗАТОЧЕННУЮ вещь: атомарно вынимает её из nest_enchanted
        продавца ($pull по id) и создаёт лот с её уровнем. None — вещи нет."""
        from pymongo import ReturnDocument
        before = await self.users.find_one_and_update(
            {"_id": seller_id, "nest_enchanted.id": item_id},
            {"$pull": {"nest_enchanted": {"id": item_id}}, "$inc": {"ops": 1}},
            projection={"nest_enchanted": 1}, return_document=ReturnDocument.BEFORE,
        )
        item = next((it for it in ((before or {}).get("nest_enchanted") or [])
                     if isinstance(it, dict) and it.get("id") == item_id), None)
        if not item:
            return None
        listing = {
            "seller_id": seller_id, "seller_name": seller_name, "item_type": item["type"], "grade": item["grade"],
            "enchant": int(item.get("enchant") or 0), "price_gram": price_gram, "created_at": ts,
        }
        try:
            result = await self.equip_market.insert_one(listing)
        except Exception:
            await self._give_equip_item(seller_id, listing)   # лот не записался — вещь возвращается
            raise
        return str(result.inserted_id)

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
        try:
            result = await self.equip_market.insert_one(listing)
        except Exception:
            # лот не записался — предмет не должен пропасть
            await self.users.update_one({"_id": seller_id}, {"$inc": {field: 1, "ops": 1}})
            raise
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

        refused = await self._buy_precheck(self.equip_market, oid, buyer_id)
        if refused:
            return refused
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

        await self._give_equip_item(buyer_id, listing)   # вместе с уровнем заточки

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

        await self._give_equip_item(seller_id, listing)   # вместе с уровнем заточки
        return "ok"

    # --- РЫНОК → «АЛХИМИЯ»: ресурсы Охоты за ЗОЛОТО по фиксированному тарифу.
    # Ресурсы продавца списываются в main.py (CAS по ops вместе с проверкой
    # остатка) ДО создания лота и лежат в самом лоте; при снятии — обратно
    # на Склад Охоты. Золото покупателя списывается атомарно ($gte), продавцу
    # зачисляется цена минус комиссия рынка. Порядок «проверка → захват лота →
    # списание → откат при отказе» — как у остальных отделов рынка. ---

    async def create_alchemy_listing(self, seller_id: int, item: str, quantity: int,
                                     unit_price: float, ts: int) -> str:
        listing = {
            "seller_id": seller_id, "item": item, "quantity": int(quantity),
            "unit_price_gold": float(unit_price), "total_price_gold": round(float(unit_price) * int(quantity), 6),
            "created_at": ts,
        }
        result = await self.alchemy_market.insert_one(listing)
        return str(result.inserted_id)

    async def _give_hunt_item(self, user_id: int, item: str, quantity: int) -> None:
        # ops: 1 — иначе параллельное действие Охоты (CAS по ops с $set всего
        # hunt_items) затёрло бы зачисление
        await self.users.update_one({"_id": user_id}, {"$inc": {f"hunt_items.{item}": int(quantity), "ops": 1}})

    async def buy_alchemy_listing(self, buyer_id: int, listing_id: str, commission: float = 0.0):
        """Словарь лота (с seller_credit — сколько золота ушло продавцу после
        комиссии) при успехе или код отказа: not_found / own_listing /
        insufficient_funds."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"
        lot = await self.alchemy_market.find_one({"_id": oid}, {"seller_id": 1, "total_price_gold": 1})
        if not lot:
            return "not_found"
        if int(lot["seller_id"]) == int(buyer_id):
            return "own_listing"
        doc = await self.users.find_one({"_id": buyer_id}, {"gold": 1})
        if not doc or float(doc.get("gold") or 0) < float(lot.get("total_price_gold") or 0):
            return "insufficient_funds"

        listing = await self.alchemy_market.find_one_and_delete({"_id": oid})
        if not listing:
            return "not_found"
        price = float(listing["total_price_gold"])
        result = await self.users.update_one(
            {"_id": buyer_id, "gold": {"$gte": price}},
            {"$inc": {"gold": -price, "ops": 1}},
        )
        if result.modified_count == 0:
            await self.alchemy_market.insert_one(listing)
            return "insufficient_funds"
        await self._give_hunt_item(buyer_id, listing["item"], listing["quantity"])
        seller_credit = round(price * (1 - commission), 9)
        await self.users.update_one(
            {"_id": listing["seller_id"]},
            {"$inc": {"gold": seller_credit, "ops": 1}},
        )
        listing["id"] = str(listing.pop("_id"))
        listing["seller_credit"] = seller_credit
        return listing

    async def cancel_alchemy_listing(self, seller_id: int, listing_id: str) -> str:
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(listing_id)
        except InvalidId:
            return "not_found"
        listing = await self.alchemy_market.find_one_and_delete({"_id": oid, "seller_id": seller_id})
        if not listing:
            exists = await self.alchemy_market.find_one({"_id": oid})
            return "not_owner" if exists else "not_found"
        await self._give_hunt_item(seller_id, listing["item"], listing["quantity"])
        return "ok"

    # --- РЫНОК РЕСУРСОВ: P2P-торговля целыми Небесными Осколками и целыми
    # частичками. В отличие от рынка снаряжения выше, списание/зачисление
    # самого ресурса делает НЕ этот класс, а main.py._adjust_user_resource —
    # Осколки завязаны на игровую формулу ставки накопления частичек
    # (nest_settle_particles), которой в storage.py нет и быть не должно.
    # Эти методы отвечают только за лот и GRAM, ровно как buy_listing/
    # cancel_listing выше отвечают только за орла и GRAM. ---

    async def create_resource_listing(self, seller_id: int, seller_name: str, resource: str,
                                       amount: int, price_gram: float, ts: int,
                                       price_gold: Optional[float] = None, unit_price_gold: Optional[float] = None) -> str:
        """Просто создаёт лот — ресурс у продавца уже списан вызывающим
        кодом (main.py._adjust_user_resource) ДО этого вызова. price_gold —
        лот за ЗОЛОТО (Частички): currency="gold", price_gold = итог лота,
        unit_price = цена за 1 шт. в золоте; price_gram тогда 0."""
        listing = {
            "seller_id": seller_id, "seller_name": seller_name, "resource": resource,
            "amount": amount, "price_gram": price_gram, "created_at": ts,
            # Цена за штуку — по ней вкладка сортирует лоты и выбирает «лучшую
            # цену»: лот «20 шт. за 0.2» выгоднее, чем «1 шт. за 0.1».
            "unit_price": price_gram / max(1, int(amount)),
        }
        if price_gold is not None:
            listing.update({"currency": "gold", "price_gram": 0.0, "price_gold": float(price_gold),
                            "unit_price": float(unit_price_gold if unit_price_gold is not None else price_gold / max(1, int(amount)))})
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

        lot = await self.resource_market.find_one({"_id": oid}, {"currency": 1, "price_gold": 1, "seller_id": 1})
        if lot and lot.get("currency") == "gold":
            # лот за ЗОЛОТО (Частички) — та же предпроверка, но по золоту
            if int(lot["seller_id"]) == int(buyer_id):
                return "own_listing"
            doc = await self.users.find_one({"_id": buyer_id}, {"gold": 1})
            if not doc or float(doc.get("gold") or 0) < float(lot.get("price_gold") or 0):
                return "insufficient_funds"
        else:
            refused = await self._buy_precheck(self.resource_market, oid, buyer_id)
            if refused:
                return refused
        listing = await self.resource_market.find_one_and_delete({"_id": oid})
        if not listing:
            return "not_found"
        if int(listing["seller_id"]) == int(buyer_id):
            await self.resource_market.insert_one(listing)
            return "own_listing"

        field, price = resource_lot_price(listing)
        result = await self.users.update_one(
            {"_id": buyer_id, field: {"$gte": price}},
            {"$inc": {field: -price, "ops": 1}},
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
        field, price = resource_lot_price(listing)
        await self.users.update_one({"_id": buyer_id}, {"$inc": {field: price, "ops": 1}})
        await self.restore_resource_listing(listing)

    async def restore_resource_listing(self, listing: dict) -> None:
        doc = dict(listing)
        listing_id = doc.pop("id", None)
        if listing_id is not None:
            from bson import ObjectId
            doc["_id"] = ObjectId(listing_id)
        await self.resource_market.insert_one(doc)

    async def credit_resource_seller(self, seller_id: int, price_gram: float, commission: float,
                                     currency: str = "gram") -> float:
        """Продавцу — цена минус комиссия: GRAM (coins + total_earned) или,
        для лотов за золото (Частички), золото. Возвращает зачисленное."""
        if currency == "gold":
            seller_credit = round(price_gram * (1 - commission), 9)
            await self.users.update_one({"_id": seller_id}, {"$inc": {"gold": seller_credit, "ops": 1}})
            return seller_credit
        seller_credit = price_gram * (1 - commission)
        await self.users.update_one(
            {"_id": seller_id},
            {"$inc": {"coins": seller_credit, "total_earned": seller_credit, "ops": 1}},
        )
        return seller_credit

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
                {"_id": tx_hash, "user_id": user_id, "amount": gram, "ts": ts, "credited_at": int(time.time()),
                 "referrer_id": referrer_id if referral_gram > 0 else None,
                 "referral_gram": float(referral_gram) if referrer_id else 0.0}
            )
        except DuplicateKeyError:
            return False
        await self.users.update_one({"_id": user_id}, {"$inc": {"coins": gram, "ops": 1}})
        if referrer_id and referral_gram > 0:
            await self.users.update_one(
                {"_id": referrer_id}, {"$inc": {"coins": referral_gram, "ops": 1}}
            )
        return True

    async def record_below_min_deposit(self, tx_hash: str, user_id: int, ton: float, ts: int) -> bool:
        """Перевод меньше минимального пополнения: GRAM не зачисляем, но
        запоминаем по хэшу, чтобы не обрабатывать (и не уведомлять игрока)
        повторно и чтобы админ видел его во вкладке «Транзакции».
        True — записан впервые."""
        from pymongo.errors import DuplicateKeyError
        try:
            await self.deposits.insert_one({
                "_id": tx_hash, "user_id": user_id, "amount": 0.0, "ton": float(ton), "ts": ts,
                "credited_at": int(time.time()), "below_min": True,
            })
            return True
        except DuplicateKeyError:
            return False

    async def count_deposits(self) -> int:
        return await self.deposits.count_documents({})

    async def list_transactions(self, kind: str = "all", user_id: Optional[int] = None,
                                limit: int = 50, offset: int = 0) -> dict:
        """Админка → Транзакции: пополнения и выводы одним списком, новые
        сверху (по дате перевода / заявки). Возвращает и итоги по фильтру."""
        q = {"user_id": user_id} if user_id is not None else {}
        take = offset + limit + 1
        rows = []
        if kind in ("all", "deposit"):
            async for d in self.deposits.find(q).sort("ts", -1).limit(take):
                rows.append({
                    "kind": "deposit", "id": str(d["_id"]), "user_id": d.get("user_id"),
                    "amount": float(d.get("amount") or 0), "ts": int(d.get("ts") or 0),
                    "below_min": bool(d.get("below_min")), "ton_sent": d.get("ton"),
                    "credited_at": d.get("credited_at"), "reversed": bool(d.get("reversed")),
                    "reversed_at": d.get("reversed_at"), "referrer_id": d.get("referrer_id"),
                    "referral_gram": float(d.get("referral_gram") or 0),
                })
        if kind in ("all", "withdrawal"):
            async for w in self.withdrawals.find(q).sort("ts", -1).limit(take):
                rows.append({
                    "kind": "withdrawal", "id": str(w["_id"]), "user_id": w.get("user_id"),
                    "amount": float(w.get("amount") or 0), "payout": float(w.get("payout") or w.get("amount") or 0),
                    "address": w.get("address") or "", "status": w.get("status") or "", "ts": int(w.get("ts") or 0),
                })
        rows.sort(key=lambda r: r["ts"], reverse=True)
        page = rows[offset:offset + limit]

        totals = {"deposits_count": 0, "deposits_gram": 0.0, "reversed_count": 0, "reversed_gram": 0.0,
                  "below_min_count": 0, "below_min_ton": 0.0}
        async for row in self.deposits.aggregate([{"$match": {**q, "below_min": True}}, {"$group": {
                "_id": None, "n": {"$sum": 1}, "ton": {"$sum": "$ton"}}}]):
            totals["below_min_count"], totals["below_min_ton"] = row["n"], float(row["ton"] or 0)
        async for row in self.deposits.aggregate([{"$match": {**q, "below_min": {"$ne": True}}}, {"$group": {
                "_id": {"$ifNull": ["$reversed", False]}, "n": {"$sum": 1}, "gram": {"$sum": "$amount"}}}]):
            if row["_id"]:
                totals["reversed_count"] += row["n"]; totals["reversed_gram"] += float(row["gram"] or 0)
            else:
                totals["deposits_count"] += row["n"]; totals["deposits_gram"] += float(row["gram"] or 0)
        for st in ("pending", "approved", "rejected"):
            totals[f"withdrawals_{st}_count"] = 0
            totals[f"withdrawals_{st}_gram"] = 0.0
        async for row in self.withdrawals.aggregate([{"$match": q}, {"$group": {
                "_id": "$status", "n": {"$sum": 1}, "gram": {"$sum": "$amount"}}}]):
            if row["_id"] in ("pending", "approved", "rejected"):
                totals[f"withdrawals_{row['_id']}_count"] = row["n"]
                totals[f"withdrawals_{row['_id']}_gram"] = float(row["gram"] or 0)
        return {"items": page, "has_more": len(rows) > offset + limit, "totals": totals}

    async def reverse_deposit(self, tx_hash: str, now: float) -> dict:
        """Отмена ошибочного зачисления (например, повтор старого перевода после
        очистки базы): пометка reversed ставится атомарно, поэтому повторное
        нажатие ничего не спишет второй раз. Списываем у игрока сумму
        депозита и у пригласившего — его реферальный бонус, если он записан
        в депозите. Баланс может уйти в минус, если GRAM уже потрачены."""
        below = await self.deposits.find_one({"_id": tx_hash, "below_min": True}, {"_id": 1})
        if below:
            return {"status": "below_min"}
        doc = await self.deposits.find_one_and_update(
            {"_id": tx_hash, "reversed": {"$ne": True}, "below_min": {"$ne": True}},
            {"$set": {"reversed": True, "reversed_at": int(now)}},
        )
        if not doc:
            exists = await self.deposits.find_one({"_id": tx_hash}, {"_id": 1})
            return {"status": "already_reversed" if exists else "not_found"}
        amount = float(doc.get("amount") or 0)
        await self.users.update_one({"_id": doc["user_id"]}, {"$inc": {"coins": -amount, "ops": 1}})
        ref_id, ref_gram = doc.get("referrer_id"), float(doc.get("referral_gram") or 0)
        if ref_id and ref_gram > 0:
            await self.users.update_one({"_id": ref_id}, {"$inc": {"coins": -ref_gram, "ops": 1}})
        return {"status": "ok", "user_id": doc["user_id"], "amount": amount,
                "referrer_id": ref_id if ref_gram > 0 else None, "referral_gram": ref_gram}

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
            below = bool(doc.get("below_min"))
            rows.append({"kind": "deposit", "amount": doc.get("ton", 0.0) if below else doc.get("amount", 0.0),
                         "ts": doc.get("ts", 0),
                         "status": "below_min" if below else ("reversed" if doc.get("reversed") else "")})
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
            {"$match": {"_id": {"$ne": exclude_user_id}, "is_bot": {"$ne": True}, "_rating": {"$gt": my_rating}}},
            {"$sort": {"_rating": 1}},
            {"$limit": above_count},
            {"$project": {"name": 1, "monsters": 1, "nest_equipped": 1, "pvp_rating": "$_rating"}},
        ]
        nearby_pipeline = [
            {"$addFields": {"_rating": {"$ifNull": ["$pvp_rating", 1000]}}},
            {"$match": {
                "_id": {"$ne": exclude_user_id}, "is_bot": {"$ne": True},
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

    async def get_leaderboard(self, limit: Optional[int] = 100) -> list:
        """Топ-N реальных игроков по pvp_rating, по убыванию — общая на всех
        Таблица лидеров Арены; limit=None возвращает ВСЕХ игроков (см.
        arena_leaderboard в main.py, которая показывает всех, тогда как
        distribute_arena_rewards по-прежнему просит только 50 — для наград
        нужен именно Топ-50, а не вся таблица). Отсутствующий pvp_rating
        (аккаунты до Арены) трактуется как стартовые 1000, как и в
        pvp_rating_of на сервере. Редкость орла (monsters) сюда намеренно
        не проецируется — таблица показывает только место/ник/рейтинг, а
        награды за призовые места читают monsters отдельно, только для
        того самого игрока, а не для всей таблицы разом."""
        pipeline = [
            {"$match": {"is_bot": {"$ne": True}}},  # тестовые клан-боты — не игроки Арены
            {"$addFields": {"_rating": {"$ifNull": ["$pvp_rating", 1000]}}},
            {"$sort": {"_rating": -1}},
        ]
        if limit is not None:
            pipeline.append({"$limit": limit})
        pipeline.append({"$project": {"name": 1, "pvp_rating": "$_rating", "faction_id": 1, "last_seen": 1}})
        docs = []
        async for doc in self.users.aggregate(pipeline):
            docs.append({
                "user_id": doc["_id"], "name": doc.get("name") or "",
                "pvp_rating": doc.get("pvp_rating"), "faction_id": doc.get("faction_id"),
                "last_seen": doc.get("last_seen"),   # наружу — только админам (см. arena_leaderboard)
            })
        return docs

    async def arena_admin_list(self, search: str = "", limit: int = 100) -> list:
        """Админка → Арена: игроки по убыванию рейтинга (боты не в счёт),
        с энергией и временем последнего захода; search — имя или ID."""
        match = {"is_bot": {"$ne": True}}
        q = self._search_query(search)
        if q:
            match = {"$and": [match, q]}
        pipeline = [
            {"$match": match},
            {"$addFields": {"_rating": {"$ifNull": ["$pvp_rating", 1000]}}},
            {"$sort": {"_rating": -1, "_id": 1}},
            {"$limit": int(limit)},
            {"$project": {"name": 1, "_rating": 1, "pvp_energy": 1, "pvp_energy_day": 1, "last_seen": 1}},
        ]
        out = []
        async for d in self.users.aggregate(pipeline):
            out.append({"user_id": d["_id"], "name": d.get("name") or "", "pvp_rating": float(d.get("_rating") or 0),
                        "pvp_energy": d.get("pvp_energy"), "pvp_energy_day": d.get("pvp_energy_day"),
                        "last_seen": d.get("last_seen")})
        return out

    async def count_arena_players(self) -> int:
        return await self.users.count_documents({"is_bot": {"$ne": True}})

    async def admin_set_arena_player(self, user_id: int, fields: dict) -> bool:
        res = await self.users.update_one({"_id": user_id, "is_bot": {"$ne": True}}, {"$set": fields, "$inc": {"ops": 1}})
        return res.matched_count > 0

    async def count_higher_rating(self, rating: float) -> int:
        """Сколько игроков строго выше данного рейтинга — чтобы посчитать
        место игрока, не попавшего в Топ-100."""
        pipeline = [
            {"$addFields": {"_rating": {"$ifNull": ["$pvp_rating", 1000]}}},
            {"$match": {"_rating": {"$gt": rating}, "is_bot": {"$ne": True}}},
            {"$count": "n"},
        ]
        async for doc in self.users.aggregate(pipeline):
            return doc["n"]
        return 0

    async def get_arena_season(self) -> Optional[dict]:
        return await self.arena_season.find_one({"_id": "global"})

    async def init_arena_season(self, doc: dict) -> dict:
        """Расписание сезона Арены: {season, started_at, ends_at, days}.
        Создаёт документ, если его нет, и дописывает started_at/ends_at/days
        к старому формату {season} (когда сезон считался формулой от даты)."""
        await self.arena_season.update_one({"_id": "global"}, {"$setOnInsert": dict(doc)}, upsert=True)
        cur = await self.arena_season.find_one({"_id": "global"})
        if cur.get("ends_at") is None:
            await self.arena_season.update_one(
                {"_id": "global", "ends_at": {"$exists": False}},
                {"$set": {k: doc[k] for k in ("started_at", "ends_at", "days")}},
            )
            cur = await self.arena_season.find_one({"_id": "global"})
        return cur

    async def advance_arena_season(self, season: int, ends_at: float, new_started: float,
                                   new_ends: float, days: int) -> bool:
        """Смена сезона: true ровно у ОДНОГО из конкурентных вызовов — тот и
        раздаёт награды и сбрасывает рейтинг (см. reconcile_arena_season)."""
        res = await self.arena_season.update_one(
            {"_id": "global", "season": season, "ends_at": ends_at},
            {"$set": {"season": season + 1, "started_at": new_started, "ends_at": new_ends, "days": days}},
        )
        return res.modified_count > 0

    async def set_arena_season_schedule(self, days: int, ends_at: Optional[float] = None) -> None:
        fields = {"days": int(days)}
        if ends_at is not None:
            fields["ends_at"] = float(ends_at)
        await self.arena_season.update_one({"_id": "global"}, {"$set": fields})

    async def reset_all_pvp_ratings(self, start_rating: int) -> None:
        """Сбрасывает PvP-рейтинг ВСЕХ игроков разом — конец сезона Арены
        (см. reconcile_arena_season). Редкая операция (раз в
        ARENA_SEASON_DAYS дней), поэтому обычный update_many без CAS —
        рейтинг никто параллельно не читает так, чтобы гонка была заметна."""
        await self.users.update_many({}, {"$set": {"pvp_rating": start_rating}})

    # --- КЛАНЫ: создание/вступление, открытие мест, сжигание орлов за
    # силу клана, расстановка бойцов, турнирная сетка Топ-32. ---

    async def create_clan(self, user_id: int, name: str, cost_gram: float, cost_gold: float,
                           cost_meat: float, open_slots: int) -> tuple:
        """Создаёт клан и сразу вступает в него создателем-лидером. Порядок
        важен: сперва создаём документ клана (сам по себе он ничего не
        стоит), затем ОДНИМ атомарным update списываем ресурсы у игрока И
        проставляем ему clan_id — если это не удалось (не хватает
        ресурсов или игрок уже успел вступить в другой клан параллельно),
        откатываем и удаляем только что созданный клан. Возвращает
        (код, clan_id|None)."""
        clan_doc = {
            "name": name, "leader_id": user_id, "members": [user_id],
            "open_slots": open_slots, "created_at": time.time(),
            "lineup_submissions": {}, "approved_lineup": [], "applications": [],
        }
        result = await self.clans.insert_one(clan_doc)
        clan_id = str(result.inserted_id)

        charge = await self.users.update_one(
            {"_id": user_id, "clan_id": {"$in": [None, ""]},
             "coins": {"$gte": cost_gram}, "gold": {"$gte": cost_gold}, "mnstr": {"$gte": cost_meat}},
            {"$inc": {"coins": -cost_gram, "gold": -cost_gold, "mnstr": -cost_meat, "ops": 1},
             "$set": {"clan_id": clan_id}},
        )
        if charge.modified_count == 0:
            await self.clans.delete_one({"_id": result.inserted_id})
            return "failed", None

        # Игрок теперь сам лидер — его старые заявки на вступление в чужие
        # кланы (если подавал несколько) больше не имеют смысла, чистим сразу.
        await self.clans.update_many({}, {"$pull": {"applications": user_id}})
        return "ok", clan_id

    async def _sum_burned_power(self, member_ids: list) -> float:
        """Сила клана считается на лету — сумма личного burned_power (см.
        документ игрока) всех перечисленных (ТЕКУЩИХ) участников."""
        if not member_ids:
            return 0.0
        total = 0.0
        async for row in self.users.find({"_id": {"$in": member_ids}}, {"burned_power": 1}):
            total += float(row.get("burned_power") or 0)
        return total

    async def get_clan(self, clan_id: str) -> Optional[dict]:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return None
        doc = await self.clans.find_one({"_id": oid})
        if not doc:
            return None
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id"))
        doc["clan_power"] = await self._sum_burned_power(doc.get("members") or [])
        return doc

    async def list_top_clans(self, limit: int = 100) -> list:
        """Топ кланов по силе — $lookup считает clan_power каждого клана
        прямо в базе (сумма burned_power его текущих members) и сортирует
        по нему, так что подгружать/суммировать участников на стороне
        Python (и держать поле clan_power синхронным вручную) не нужно."""
        pipeline = [
            {"$lookup": {
                "from": "users", "localField": "members", "foreignField": "_id", "as": "_members",
            }},
            {"$addFields": {"clan_power": {"$sum": "$_members.burned_power"}}},
            {"$project": {"_members": 0}},
            {"$sort": {"clan_power": -1}},
            {"$limit": limit},
        ]
        out = []
        async for doc in self.clans.aggregate(pipeline):
            doc = dict(doc)
            doc["id"] = str(doc.pop("_id"))
            out.append(doc)
        return out

    async def apply_to_clan(self, user_id: int, clan_id: str) -> str:
        """Подаёт заявку на вступление — не вступает сразу, ждёт решения
        клан-лидера (см. accept_clan_application/reject_clan_application).
        $addToSet — повторная подача той же заявки идемпотентна, не плодит
        дубликаты в списке."""
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return "not_found"
        result = await self.clans.update_one(
            {"_id": oid}, {"$addToSet": {"applications": user_id}},
        )
        return "ok" if result.matched_count > 0 else "not_found"

    async def accept_clan_application(self, clan_id: str, leader_id: int, applicant_id: int,
                                       member_limit: int) -> str:
        """Лидер принимает заявку — заявитель переходит из applications в
        members, только если в клане ещё есть открытое место. Порядок как у
        create_clan: сперва двигаем заявителя внутри клана, затем отдельным
        атомарным update проставляем ему clan_id; если он уже успел вступить
        куда-то ещё (гонка), откатываем members обратно."""
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return "not_found"

        result = await self.clans.update_one(
            {"_id": oid, "leader_id": leader_id, "applications": applicant_id,
             "$expr": {"$lt": [{"$size": "$members"}, "$open_slots"]}},
            {"$push": {"members": applicant_id}, "$pull": {"applications": applicant_id}},
        )
        if result.modified_count == 0:
            clan = await self.clans.find_one({"_id": oid})
            if not clan:
                return "not_found"
            if clan.get("leader_id") != leader_id:
                return "not_leader"
            if applicant_id not in (clan.get("applications") or []):
                return "not_applied"
            return "no_open_slot"

        charge = await self.users.update_one(
            {"_id": applicant_id, "clan_id": {"$in": [None, ""]}},
            {"$set": {"clan_id": clan_id}},
        )
        if charge.modified_count == 0:
            await self.clans.update_one({"_id": oid}, {"$pull": {"members": applicant_id}})
            return "already_in_clan"
        return "ok"

    async def reject_clan_application(self, clan_id: str, leader_id: int, applicant_id: int) -> bool:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return False
        result = await self.clans.update_one(
            {"_id": oid, "leader_id": leader_id}, {"$pull": {"applications": applicant_id}},
        )
        return result.modified_count > 0

    async def leave_clan(self, user_id: int, clan_id: str) -> str:
        """Выход из клана. Если уходит лидер и в клане остаётся кто-то ещё,
        лидерство переходит следующему по списку участников — без этого
        клан осиротел бы без единого способа управлять местами/составом.
        Если участников не осталось вовсе — клан удаляется."""
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return "not_found"

        # Запись — только если состав и лидер те же, что мы прочитали:
        # иначе при одновременном выходе лидера и его «наследника» (или
        # исключении наследника) лидером становился уже ушедший игрок и клан
        # оставался без управления, а при выходе двух последних — висел
        # пустым. Не совпало — перечитываем и пробуем снова.
        for _ in range(8):
            clan = await self.clans.find_one({"_id": oid})
            if not clan or user_id not in (clan.get("members") or []):
                return "not_member"

            members = [m for m in clan["members"] if m != user_id]
            guard = {"_id": oid, "members": clan["members"], "leader_id": clan.get("leader_id")}
            if not members:
                done = (await self.clans.delete_one(guard)).deleted_count > 0
            else:
                update = {"$set": {"members": members}, "$unset": {f"lineup_submissions.{user_id}": ""}}
                if clan.get("leader_id") == user_id:
                    update["$set"]["leader_id"] = members[0]
                done = (await self.clans.update_one(guard, update)).modified_count > 0
            if done:
                await self.users.update_one({"_id": user_id, "clan_id": clan_id}, {"$set": {"clan_id": None}})
                return "ok"
        return "busy"

    async def disband_clan(self, leader_id: int, clan_id: str) -> str:
        """Лидер распускает клан целиком — клан удаляется, ВСЕ участники
        (включая самого лидера) теряют clan_id. find_one_and_delete с
        условием leader_id атомарно совмещает проверку прав и удаление:
        под гонкой (например, параллельный leave_clan последнего
        участника) удаление сработает только один раз."""
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return "not_found"

        clan = await self.clans.find_one_and_delete({"_id": oid, "leader_id": leader_id})
        if not clan:
            still_exists = await self.clans.find_one({"_id": oid})
            if not still_exists:
                return "not_found"
            return "not_leader"

        members = clan.get("members") or []
        if members:
            await self.users.update_many({"_id": {"$in": members}}, {"$set": {"clan_id": None}})
        return "ok"

    async def kick_clan_member(self, leader_id: int, clan_id: str, target_id: int) -> str:
        """Лидер исключает участника из клана. Лидер не может исключить
        сам себя (для этого есть leave_clan, с передачей лидерства) —
        проверяем это до похода в базу, иначе conditional update ниже
        бы совпал и вышвырнул лидера без всякой передачи лидерства."""
        if target_id == leader_id:
            return "cannot_kick_self"
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return "not_found"

        result = await self.clans.update_one(
            {"_id": oid, "leader_id": leader_id, "members": target_id},
            {"$pull": {"members": target_id}, "$unset": {f"lineup_submissions.{target_id}": ""}},
        )
        if result.modified_count == 0:
            clan = await self.clans.find_one({"_id": oid})
            if not clan:
                return "not_found"
            if clan.get("leader_id") != leader_id:
                return "not_leader"
            if target_id not in (clan.get("members") or []):
                return "not_member"
            return "failed"

        await self.users.update_one({"_id": target_id, "clan_id": clan_id}, {"$set": {"clan_id": None}})
        return "ok"

    async def get_setting(self, key: str, default=None):
        doc = await self.settings.find_one({"_id": key})
        return doc.get("value", default) if doc else default

    async def set_setting(self, key: str, value) -> None:
        await self.settings.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)

    async def reserve_nft_claim(self, nft: str, user_id: int, now: float, interval: float):
        """Атомарно занимает NFT для сбора: удаётся, только если с прошлого
        сбора по ЭТОЙ NFT прошло не меньше interval. Возвращает прежнее
        last_claim (0 — NFT ещё ни разу не собирали) для отката через
        release_nft_claim, либо False — NFT ещё на таймере."""
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError
        try:
            before = await self.nft_claims.find_one_and_update(
                {"_id": nft, "last_claim": {"$lte": now - interval}},
                {"$set": {"last_claim": now, "user_id": user_id}},
                return_document=ReturnDocument.BEFORE,
            )
            if before:
                return float(before.get("last_claim") or 0)
            await self.nft_claims.insert_one({"_id": nft, "last_claim": now, "user_id": user_id})
            return 0.0
        except DuplicateKeyError:
            return False   # запись есть, и таймер NFT ещё не прошёл

    async def release_nft_claim(self, nft: str, previous: float) -> None:
        if previous:
            await self.nft_claims.update_one({"_id": nft}, {"$set": {"last_claim": previous}})
        else:
            await self.nft_claims.delete_one({"_id": nft})

    async def nft_claim_wait(self, nfts: list, now: float, interval: float) -> float:
        """Сколько ждать до освобождения ближайшей из NFT."""
        waits = []
        async for doc in self.nft_claims.find({"_id": {"$in": list(nfts)}}):
            waits.append(float(doc.get("last_claim") or 0) + interval - now)
        return max(0.0, min(waits)) if waits else 0.0

    # --- АУКЦИОН ---
    # Деньги и таблица лидеров — разные документы, транзакций нет, поэтому
    # каждый шаг атомарен сам по себе и безопасно повторяем:
    #   * заморозка — один update игрока: coins -= разница, auction_holds.<id> =
    #     новая ставка (условие: денег хватает и прежняя заморозка та, что в ТОП-5);
    #   * ТОП-5 — CAS по version лота; вместе с ним в pending_refunds пишется
    #     выбывший 6-й, так что его возврат не теряется даже при падении сервера;
    #   * возврат — update игрока «только если auction_holds.<id> == сумма»:
    #     повторный вызов ничего не начислит второй раз.

    @staticmethod
    def _auction_oid(auction_id):
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            return ObjectId(str(auction_id))
        except (InvalidId, TypeError):
            return None

    @staticmethod
    def _auction_doc(doc: Optional[dict]) -> Optional[dict]:
        if not doc:
            return None
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id"))
        return doc

    async def create_auction(self, title: str, item_image: str, description: str,
                             min_bid: float, step: float, now: float, ends_at: float,
                             reward_type: str = "nft", reward_amount: float = 1,
                             max_places: int = 5) -> Optional[dict]:
        """Новый лот. Одновременно активен только один: замок active_auction в
        settings занимается атомарно (как reserve_nft_claim). None — уже идёт другой."""
        from bson import ObjectId
        from pymongo.errors import DuplicateKeyError
        oid = ObjectId()
        try:
            await self.settings.update_one(
                {"_id": "active_auction", "value": None}, {"$set": {"value": str(oid)}}, upsert=True,
            )
        except DuplicateKeyError:
            return None
        doc = {
            "_id": oid, "title": title, "item_image": item_image, "description": description,
            "min_bid": float(min_bid), "step": float(step), "created_at": now, "ends_at": float(ends_at),
            "status": "active", "version": 0, "top": [], "pending_refunds": [],
            "settled": False, "winners": [],
            # Что получает КАЖДЫЙ из топа: nft (ручная отправка админом) или
            # ресурс (sky_shards / gold / meat) в количестве reward_amount.
            "reward_type": reward_type, "reward_amount": float(reward_amount),
            # Сколько штук разыгрывается = сколько мест в живой очереди:
            # (max_places + 1)-й выбывает с возвратом, топ забирает лоты.
            "max_places": int(max_places),
        }
        await self.auctions.insert_one(doc)
        return self._auction_doc(doc)

    async def get_auction(self, auction_id) -> Optional[dict]:
        oid = self._auction_oid(auction_id)
        return self._auction_doc(await self.auctions.find_one({"_id": oid})) if oid else None

    async def get_active_auction(self) -> Optional[dict]:
        return self._auction_doc(await self.auctions.find_one({"status": "active"}, sort=[("created_at", -1)]))

    async def get_latest_auction(self) -> Optional[dict]:
        return self._auction_doc(await self.auctions.find_one({}, sort=[("created_at", -1)]))

    async def list_auctions(self, limit: int = 10) -> list:
        return [self._auction_doc(d) async for d in self.auctions.find({}).sort("created_at", -1).limit(limit)]

    async def auctions_with_pending_refunds(self) -> list:
        return [str(d["_id"]) async for d in self.auctions.find(
            {"pending_refunds.0": {"$exists": True}}, {"_id": 1})]

    async def list_due_auctions(self, now: float) -> list:
        """Лоты, которым пора закрыться или довести расчёт до конца."""
        query = {"$or": [{"status": "active", "ends_at": {"$lte": now}},
                         {"status": {"$in": ["finished", "cancelled"]}, "settled": False}]}
        return [self._auction_doc(d) async for d in self.auctions.find(query)]

    async def _refund_hold(self, user_id: int, key: str, amount: float) -> bool:
        """Возврат заморозки ровно один раз: только пока она ещё == amount."""
        with ledger_source("auction:refund"):
            res = await self.users.update_one(
                {"_id": user_id, f"auction_holds.{key}": amount},
                {"$unset": {f"auction_holds.{key}": ""}, "$inc": {"coins": amount, "ops": 1}},
            )
        return res.modified_count > 0

    async def _refund_if_evicted(self, oid, user_id: int, old_hold: float) -> None:
        """Пока игрок перебивал сам себя, его могли выбить из ТОП-5. Возврат из
        pending_refunds тогда не сработал (заморозка в тот момент уже была
        новой суммой) и запись о нём ушла; после отката у игрока снова
        old_hold — без места в ТОП-5. Без этого шага GRAM висел бы
        замороженным до конца лота, а сам игрок не смог бы поставить снова."""
        doc = await self.auctions.find_one({"_id": oid}, {"top": 1, "pending_refunds": 1}) or {}
        in_top = any(int(e["user_id"]) == user_id and abs(float(e["bid"]) - old_hold) < 1e-9
                     for e in doc.get("top") or [])
        pending = any(int(r["user_id"]) == user_id for r in doc.get("pending_refunds") or [])
        if not in_top and not pending:
            await self._refund_hold(user_id, str(oid), old_hold)

    async def process_auction_refunds(self, auction_id) -> int:
        """Мгновенный возврат выбывшим из топа. Идемпотентно (см. _refund_hold)."""
        oid = self._auction_oid(auction_id)
        doc = await self.auctions.find_one({"_id": oid}) if oid else None
        if not doc:
            return 0
        done = 0
        key = str(oid)
        for r in doc.get("pending_refunds") or []:
            uid, amount = int(r["user_id"]), float(r["amount"])
            if await self._refund_hold(uid, key, amount):
                done += 1
            else:
                # Возврат не прошёл: заморозка сейчас не равна amount. Если она
                # есть, а игрока нет в ТОП-5 — у него прямо сейчас идёт своя
                # ставка, которая откатится к amount; запись оставляем, вернём
                # на следующем проходе. Иначе (заморозки нет — уже вернули;
                # игрок в ТОП-5 — его заморозка принадлежит живой ставке) запись
                # устарела и её можно убрать.
                user = await self.users.find_one({"_id": uid}, {f"auction_holds.{key}": 1}) or {}
                hold = (user.get("auction_holds") or {}).get(key)
                fresh = await self.auctions.find_one({"_id": oid}, {"top": 1}) or {}
                in_top = any(int(e["user_id"]) == uid for e in fresh.get("top") or [])
                if hold is not None and not in_top:
                    continue
            await self.auctions.update_one({"_id": oid}, {"$pull": {"pending_refunds": {
                "user_id": r["user_id"], "amount": r["amount"]}}})
        return done

    async def place_auction_bid(self, auction_id, user_id: int, name: str, expected: Optional[float],
                                now: float, top_size: int, antisnipe_seconds: float = 0) -> dict:
        """Ставка = лидер + шаг (или минимальная, если ставок нет). Игрок встаёт
        на 1-е место, остальные сдвигаются вниз, вышедший за границу топа
        (max_places лота; у старых лотов — top_size) выбывает с мгновенным
        возвратом. Если игрок уже в топе — замораживается только разница.
        Антиснайпер: если до конца меньше antisnipe_seconds, принятая ставка
        продлевает лот ровно до now + antisnipe_seconds — в той же атомарной
        записи, что и сама ставка."""
        oid = self._auction_oid(auction_id)
        if not oid:
            return {"status": "not_found"}
        key = str(oid)
        hold_path = f"auction_holds.{key}"
        for _ in range(6):
            doc = await self.auctions.find_one({"_id": oid})
            if not doc:
                return {"status": "not_found"}
            if doc.get("status") != "active" or float(doc["ends_at"]) <= now:
                return {"status": "ended"}
            if doc.get("pending_refunds"):
                await self.process_auction_refunds(key)
                doc = await self.auctions.find_one({"_id": oid})
            places = int(doc.get("max_places") or top_size)
            top = list(doc.get("top") or [])
            if top and int(top[0]["user_id"]) == user_id:
                return {"status": "already_leader"}
            required = float(top[0]["bid"]) + float(doc["step"]) if top else float(doc["min_bid"])
            required = max(required, float(doc["min_bid"]))
            if expected is not None and abs(float(expected) - required) > 1e-6:
                return {"status": "price_changed", "required": required}
            own = next((e for e in top if int(e["user_id"]) == user_id), None)
            old_hold = float(own["bid"]) if own else None
            delta = required - (old_hold or 0.0)

            user_filter = {"_id": user_id, "coins": {"$gte": delta}}
            user_filter[hold_path] = old_hold if own else {"$exists": False}
            charged = await self.users.update_one(
                user_filter, {"$inc": {"coins": -delta, "ops": 1}, "$set": {hold_path: required}},
            )
            if charged.modified_count == 0:
                user = await self.users.find_one({"_id": user_id}) or {}
                if float(user.get("coins") or 0) < delta:
                    return {"status": "insufficient", "required": required, "delta": delta}
                continue   # заморозка ещё не вернулась/гонка — перечитываем лот

            entry = {"user_id": user_id, "name": name, "bid": required, "ts": now}
            new_top = [entry] + [e for e in top if int(e["user_id"]) != user_id]
            evicted = new_top[places:]
            new_top = new_top[:places]
            update = {"$set": {"top": new_top, "version": int(doc.get("version") or 0) + 1}}
            extended_to = None
            if antisnipe_seconds and float(doc["ends_at"]) - now < antisnipe_seconds:
                extended_to = now + antisnipe_seconds
                update["$set"]["ends_at"] = extended_to
                update["$inc"] = {"extensions": 1}
            if evicted:
                update["$push"] = {"pending_refunds": {"$each": [
                    {"user_id": int(e["user_id"]), "amount": float(e["bid"])} for e in evicted]}}
            placed = await self.auctions.update_one(
                {"_id": oid, "version": int(doc.get("version") or 0), "status": "active", "ends_at": {"$gt": now}},
                update,
            )
            if placed.modified_count == 0:
                # Лот успел измениться — откатываем заморозку и пробуем заново.
                rollback = {"$inc": {"coins": delta, "ops": 1}}
                rollback["$set" if own else "$unset"] = {hold_path: old_hold if own else ""}
                await self.users.update_one({"_id": user_id, hold_path: required}, rollback)
                if own:
                    await self._refund_if_evicted(oid, user_id, old_hold)
                continue
            if evicted:
                await self.process_auction_refunds(key)
            return {"status": "ok", "bid": required, "delta": delta, "evicted": [int(e["user_id"]) for e in evicted],
                    "evicted_bids": [{"user_id": int(e["user_id"]), "bid": float(e["bid"])} for e in evicted],
                    "extended_to": extended_to}
        return {"status": "busy"}

    async def finish_auction(self, auction_id, now: float, force: bool = False) -> bool:
        """active -> finished (по таймеру или досрочно админом). После этого ни
        одна ставка уже не пройдёт (CAS ставки требует status active)."""
        oid = self._auction_oid(auction_id)
        query = {"_id": oid, "status": "active"}
        if not force:
            query["ends_at"] = {"$lte": now}
        update = {"$set": {"status": "finished", "finished_at": now}}
        if force:
            update["$set"]["ends_at"] = now
        res = await self.auctions.update_one(query, update)
        return res.modified_count > 0

    async def cancel_auction(self, auction_id, now: float) -> bool:
        oid = self._auction_oid(auction_id)
        res = await self.auctions.update_one(
            {"_id": oid, "status": "active"}, {"$set": {"status": "cancelled", "finished_at": now}},
        )
        return res.modified_count > 0

    async def set_auction_ends(self, auction_id, ends_at: float) -> bool:
        """Точно выставить время окончания идущего лота (тестовая кнопка)."""
        oid = self._auction_oid(auction_id)
        res = await self.auctions.update_one({"_id": oid, "status": "active"}, {"$set": {"ends_at": float(ends_at)}})
        return res.matched_count > 0

    async def shorten_auction(self, auction_id, ends_at: float) -> bool:
        oid = self._auction_oid(auction_id)
        res = await self.auctions.update_one(
            {"_id": oid, "status": "active", "ends_at": {"$gt": ends_at}}, {"$set": {"ends_at": ends_at}},
        )
        return res.modified_count > 0

    async def settle_auction(self, auction_id) -> Optional[dict]:
        """Итог закрытого лота: победителям (топ из max_places мест) заморозка списывается навсегда и
        в профиль добавляется выигранный предмет (auction_wins); всем остальным
        (выбывшие, отменённый лот, осиротевшая заморозка от оборванного запроса)
        — возврат. Каждый шаг идемпотентен; повторный вызов безопасен.
        Возвращает {"spent": сумма ставок победителей} тому, кто завершил расчёт."""
        oid = self._auction_oid(auction_id)
        doc = await self.auctions.find_one({"_id": oid}) if oid else None
        if not doc or doc.get("status") not in ("finished", "cancelled") or doc.get("settled"):
            return None
        key = str(oid)
        hold_path = f"auction_holds.{key}"
        await self.process_auction_refunds(key)
        winners = []
        if doc["status"] == "finished":
            places = int(doc.get("max_places") or 5)
            for place, e in enumerate((doc.get("top") or [])[:places], start=1):
                uid, bid = int(e["user_id"]), float(e["bid"])
                reward_type = doc.get("reward_type") or "nft"
                win = {"auction_id": key, "title": doc.get("title") or "", "item_image": doc.get("item_image") or "",
                       "bid": bid, "place": place, "won_at": float(doc.get("finished_at") or doc["ends_at"]),
                       "reward_type": reward_type, "reward_amount": float(doc.get("reward_amount") or 1),
                       # NFT отправляет админ вручную; ресурс начисляет сервер
                       # (pending_credit -> credited, см. credit_auction_rewards в main.py).
                       "status": "pending_delivery" if reward_type == "nft" else "pending_credit"}
                await self.users.update_one(
                    {"_id": uid, hold_path: bid, "auction_wins.auction_id": {"$ne": key}},
                    {"$unset": {hold_path: ""}, "$push": {"auction_wins": win}, "$inc": {"ops": 1}},
                )
                winners.append({"place": place, "user_id": uid, "name": e.get("name") or "", "bid": bid, "delivered": False})
        # Всё, что осталось замороженным под этим лотом, — не выигрыш: вернуть.
        async for user in self.users.find({hold_path: {"$exists": True}}):
            amount = float((user.get("auction_holds") or {}).get(key) or 0)
            await self._refund_hold(user["_id"], key, amount)
        res = await self.auctions.update_one(
            {"_id": oid, "settled": False}, {"$set": {"settled": True, "winners": winners}},
        )
        await self.settings.update_one({"_id": "active_auction", "value": key}, {"$set": {"value": None}})
        if res.modified_count == 0:
            return None
        return {"spent": sum(w["bid"] for w in winners), "winners": winners}

    async def users_with_pending_auction_rewards(self, limit: int = 200) -> list:
        return [d["_id"] async for d in self.users.find(
            {"auction_wins.status": "pending_credit"}, projection={"_id": 1}).limit(limit)]

    async def set_auction_delivery(self, auction_id, user_id: int, delivered: bool) -> bool:
        """Админ отметил, что NFT победителю отправлена вручную (или снял отметку)."""
        oid = self._auction_oid(auction_id)
        if not oid:
            return False
        res = await self.auctions.update_one(
            {"_id": oid, "winners.user_id": user_id}, {"$set": {"winners.$.delivered": bool(delivered)}},
        )
        if res.matched_count == 0:
            return False
        await self.users.update_one(
            {"_id": user_id, "auction_wins.auction_id": str(oid)},
            {"$set": {"auction_wins.$.status": "delivered" if delivered else "pending_delivery"}, "$inc": {"ops": 1}},
        )
        return True

    async def list_ledger(self, user_id: int, currency: str = "", limit: int = 100,
                          before_ts: Optional[float] = None) -> list:
        query = {"user_id": user_id}
        if currency in LEDGER_FIELDS:
            query[f"delta.{currency}"] = {"$exists": True}
        if before_ts:
            query["ts"] = {"$lt": float(before_ts)}
        out = []
        async for doc in self.balance_ledger.find(query).sort("ts", -1).limit(limit):
            out.append({"ts": doc["ts"], "first_ts": doc.get("first_ts") or doc["ts"],
                        "source": doc.get("source") or "", "count": int(doc.get("count") or 1),
                        "delta": {k: v for k, v in (doc.get("delta") or {}).items() if abs(v) > 1e-9},
                        "balance": doc.get("balance") or {}})
        return out

    async def prune_ledger(self, now: Optional[float] = None) -> int:
        """Удаляет записи журнала старше LEDGER_RETENTION_SECONDS."""
        cutoff = (time.time() if now is None else now) - LEDGER_RETENTION_SECONDS
        result = await self.balance_ledger.delete_many({"ts": {"$lt": cutoff}})
        try:
            await self.item_log.delete_many({"ts": {"$lt": (time.time() if now is None else now) - ITEM_LOG_RETENTION_SECONDS}})
        except Exception as e:
            print(f"[item_log] prune failed: {type(e).__name__}: {e}")
        return result.deleted_count

    async def list_item_log(self, user_id: int, limit: int = 100, before: Optional[float] = None) -> list:
        query = {"user_id": user_id}
        if before:
            query["ts"] = {"$lt": float(before)}
        out = []
        async for doc in self.item_log.find(query).sort("ts", -1).limit(limit):
            doc.pop("_id", None)
            out.append(doc)
        return out

    # --- RETENTION: посещения по дням ---

    async def record_activity(self, user_id: int, date: str) -> None:
        """Отмечает, что игрок заходил в игру в сутки date ('YYYY-MM-DD').
        _id = user_id:date — повторный вход в те же сутки ничего не меняет."""
        await self.user_activity.update_one(
            {"_id": f"{user_id}:{date}"}, {"$setOnInsert": {"user_id": int(user_id), "date": date}}, upsert=True,
        )

    async def backfill_activity_from_ledger(self) -> dict:
        """Посещения за дни ДО появления журнала посещений — по журналу
        балансов: день, в который у игрока менялся GRAM/Meat/золото, считается
        днём захода (нижняя оценка — заход без единого действия не виден).
        Заодно ставит created_at игрокам без него по записи «start» журнала
        (её пишет само создание игрока). Идемпотентно."""
        days = {}
        created = {}
        async for e in self.balance_ledger.find({}, {"user_id": 1, "ts": 1, "first_ts": 1, "source": 1}):
            uid = e.get("user_id")
            if uid is None:
                continue
            for t in (e.get("first_ts"), e.get("ts")):
                if t:
                    days.setdefault(uid, set()).add(time.strftime("%Y-%m-%d", time.gmtime(float(t))))
            if e.get("source") == "start" and (e.get("first_ts") or e.get("ts")):
                t0 = float(e.get("first_ts") or e.get("ts"))
                created[uid] = min(created.get(uid, t0), t0)
        pairs = [(uid, d) for uid, ds in days.items() for d in ds]
        for i in range(0, len(pairs), 100):
            await asyncio.gather(*(self.record_activity(uid, d) for uid, d in pairs[i:i + 100]))
        ops = pairs
        set_created = 0
        for uid, t0 in created.items():
            res = await self.users.update_one({"_id": uid, "created_at": {"$exists": False}}, {"$set": {"created_at": t0}})
            set_created += res.modified_count
        return {"activity_rows": len(ops), "created_at_set": set_created}

    async def activity_by_user(self) -> dict:
        """{user_id: set(дат)} по всем посещениям."""
        out = {}
        pipeline = [{"$group": {"_id": "$user_id", "dates": {"$addToSet": "$date"}}}]
        async for row in self.user_activity.aggregate(pipeline):
            out[row["_id"]] = set(row.get("dates") or [])
        return out

    async def created_at_by_user(self) -> dict:
        out = {}
        async for row in self.users.find({"is_bot": {"$ne": True}}, {"created_at": 1}):
            out[row["_id"]] = row.get("created_at")
        return out

    # --- ОХОТА: мониторинг ресурсов игроков (админка) ---

    HUNT_ITEM_KEYS = ("rock_crystal", "mountain_root", "shimmer_mushroom", "craft_scroll", "potion_rage", "potion_skin",
                      "blank_enchant_scroll", "enchant_scroll")
    HUNT_TOUCHED = {"$or": [{"hunt_items": {"$exists": True}}, {"mine_last": {"$gt": 0}},
                            {"hunt_slots_open": {"$gt": 0}}]}

    # --- ЗОЛОТЫЕ АКЦИИ: один документ settings/_id=gold_shares с полями
    # price (цена текущего пула, GRAM), available (осталось в пуле), sold
    # (продано всего, ≤ cap), pool_id (номер пула — меняется при каждом
    # запуске). Все изменения — атомарными операциями над этим документом:
    # инвариант sold + available ≤ cap держится при любых гонках.
    SHARES_KEY = "gold_shares"

    async def get_shares_pool(self) -> dict:
        doc = await self.settings.find_one({"_id": self.SHARES_KEY})
        if doc:
            return doc
        await self.settings.update_one(
            {"_id": self.SHARES_KEY},
            {"$setOnInsert": {"price": 0.0, "available": 0, "sold": 0, "pool_id": 0, "pool_size": 0, "started_at": 0.0}},
            upsert=True)
        return await self.settings.find_one({"_id": self.SHARES_KEY})

    async def reserve_share(self, price: float, pool_id: int, cap: int) -> Optional[dict]:
        """Берёт 1 акцию из пула, только если пул тот же, цена та же, акции
        есть и лимит эмиссии не исчерпан. None — нельзя (раскуплено/сменилось)."""
        from pymongo import ReturnDocument
        await self.get_shares_pool()
        return await self.settings.find_one_and_update(
            {"_id": self.SHARES_KEY, "pool_id": pool_id, "price": price,
             "available": {"$gt": 0}, "sold": {"$lt": cap}},
            {"$inc": {"available": -1, "sold": 1}},
            return_document=ReturnDocument.AFTER)

    async def release_share(self, pool_id: int) -> None:
        """Возврат резерва, если списать GRAM не удалось. Пул уже сменился —
        в новый пул акция не добавляется, только откатывается счётчик продаж."""
        res = await self.settings.update_one({"_id": self.SHARES_KEY, "pool_id": pool_id, "sold": {"$gt": 0}},
                                             {"$inc": {"available": 1, "sold": -1}})
        if not res.modified_count:
            await self.settings.update_one({"_id": self.SHARES_KEY, "sold": {"$gt": 0}}, {"$inc": {"sold": -1}})

    async def launch_shares_pool(self, price: float, count: int, cap: int, now: float) -> Optional[dict]:
        """Новый пул: цена и количество перезаписываются. Только если
        sold + count ≤ cap — иначе None."""
        from pymongo import ReturnDocument
        await self.get_shares_pool()
        return await self.settings.find_one_and_update(
            {"_id": self.SHARES_KEY, "sold": {"$lte": cap - count}},
            {"$set": {"price": price, "available": count, "pool_size": count, "started_at": now},
             "$inc": {"pool_id": 1}},
            return_document=ReturnDocument.AFTER)

    async def stop_shares_pool(self) -> dict:
        from pymongo import ReturnDocument
        await self.get_shares_pool()
        return await self.settings.find_one_and_update(
            # pool_id тоже меняем: возврат резерва из остановленного пула не
            # должен снова открыть в нём продажу (см. release_share).
            {"_id": self.SHARES_KEY}, {"$set": {"available": 0}, "$inc": {"pool_id": 1}},
            return_document=ReturnDocument.AFTER)

    async def list_shareholders(self, limit: int = 0) -> list:
        cur = self.users.find({"shares_owned": {"$gt": 0}},
                              {"name": 1, "shares_owned": 1, "shares_dividends_unclaimed": 1, "shares_div_day": 1,
                               "shares_dividends_claimed": 1, "vip_tier": 1, "vip_expires_at": 1}).sort("shares_owned", -1)
        if limit:
            cur = cur.limit(limit)
        return [doc async for doc in cur]

    async def list_hunt_players(self, search: str = "", limit: int = 50, offset: int = 0) -> tuple:
        """Игроки, заходившие в Охоту (или найденные поиском по id/имени), по
        убыванию суммы ресурсов и предметов на Складе."""
        query = {"is_bot": {"$ne": True}}
        search = (search or "").strip()
        if search:
            ors = [{"name": {"$regex": re.escape(search.lstrip("@")), "$options": "i"}}]
            if search.lstrip("-").isdigit():
                ors.append({"_id": int(search)})
            query["$or"] = ors
        else:
            query.update(self.HUNT_TOUCHED)
        proj = {"name": 1, "hunt_items": 1, "mine_level": 1, "mine_acc": 1, "mine_last": 1, "hunt_slots_open": 1,
                "buff_rage": 1, "buff_skin": 1, "buff_craft_discount": 1, "monsters": 1}
        rows = [r async for r in self.users.find(query, proj)]

        def weight(r):
            items = r.get("hunt_items") if isinstance(r.get("hunt_items"), dict) else {}
            return sum(int(items.get(k) or 0) for k in self.HUNT_ITEM_KEYS)

        rows.sort(key=lambda r: (-weight(r), r["_id"]))
        return rows[offset:offset + limit], len(rows)

    async def hunt_totals(self) -> dict:
        """Сумма каждого ресурса/предмета Охоты по всем игрокам."""
        totals = {k: 0 for k in self.HUNT_ITEM_KEYS}
        players = 0
        async for r in self.users.find({"is_bot": {"$ne": True}, **self.HUNT_TOUCHED}, {"hunt_items": 1}):
            players += 1
            items = r.get("hunt_items") if isinstance(r.get("hunt_items"), dict) else {}
            for k in self.HUNT_ITEM_KEYS:
                totals[k] += int(items.get(k) or 0)
        return {"players": players, "items": totals}

    async def alchemy_lot_totals(self) -> dict:
        """Сколько ресурсов Охоты сейчас «заморожено» в лотах рынка Алхимии."""
        totals = {}
        async for row in self.alchemy_market.aggregate([{"$group": {"_id": "$item", "qty": {"$sum": "$quantity"}, "lots": {"$sum": 1}}}]):
            totals[row["_id"]] = {"quantity": int(row.get("qty") or 0), "lots": int(row.get("lots") or 0)}
        return totals

    async def economy_total(self) -> dict:
        return await self.economy.find_one({"_id": "total"}) or {}

    # --- ЖУРНАЛ НАГРАД ЕЖЕДНЕВНОГО ВХОДА ---

    async def add_daily_claim(self, doc: dict) -> None:
        await self.daily_claims.insert_one(dict(doc))

    async def daily_claim_users_by_day(self) -> dict:
        """{день серии: set(user_id)} — кто по журналу забирал награду этого дня."""
        out = {}
        pipeline = [{"$group": {"_id": "$day", "users": {"$addToSet": "$user_id"}}}]
        async for row in self.daily_claims.aggregate(pipeline):
            out[int(row["_id"] or 0)] = set(row.get("users") or [])
        return out

    async def daily_claims_count(self) -> int:
        return await self.daily_claims.count_documents({})

    async def first_daily_claim_ts(self) -> Optional[float]:
        async for doc in self.daily_claims.find({}, {"ts": 1}).sort("ts", 1).limit(1):
            return doc.get("ts")
        return None

    async def recent_daily_claims(self, limit: int = 50, day: Optional[int] = None) -> list:
        query = {"day": int(day)} if day else {}
        out = []
        async for doc in self.daily_claims.find(query).sort("ts", -1).limit(limit):
            doc.pop("_id", None)
            out.append(doc)
        return out

    async def daily_progress_by_user(self) -> dict:
        """{user_id: (daily_day, daily_cycles)} живых игроков — для оценки
        выдач до появления журнала."""
        out = {}
        async for row in self.users.find({"is_bot": {"$ne": True}}, {"daily_day": 1, "daily_cycles": 1}):
            out[row["_id"]] = (int(row.get("daily_day") or 0), int(row.get("daily_cycles") or 0))
        return out

    async def add_market_trade(self, doc: dict) -> None:
        await self.market_trades.insert_one(dict(doc))

    async def list_market_trades(self, limit: int = 100, user_id: Optional[int] = None,
                                 market: str = "") -> list:
        q = {}
        if user_id is not None:
            q["$or"] = [{"buyer_id": user_id}, {"seller_id": user_id}]
        if market:
            q["market"] = market
        out = []
        async for doc in self.market_trades.find(q).sort("ts", -1).limit(limit):
            doc["id"] = str(doc.pop("_id"))
            out.append(doc)
        return out

    async def first_market_trade_ts(self) -> Optional[float]:
        doc = await self.market_trades.find_one({}, sort=[("ts", 1)])
        return float(doc["ts"]) if doc else None

    async def ledger_entries_by_sources(self, sources: list, since: float, until: Optional[float] = None,
                                        limit: int = 5000) -> list:
        q = {"source": {"$in": sources}, "ts": {"$gte": since}}
        if until is not None:
            q["ts"]["$lt"] = until
        return [doc async for doc in self.balance_ledger.find(q).sort("ts", -1).limit(limit)]

    async def names_by_ids(self, ids) -> dict:
        ids = list(ids)
        out = {}
        if not ids:
            return out
        async for row in self.users.find({"_id": {"$in": ids}}, {"name": 1, "username": 1}):
            out[row["_id"]] = row.get("name") or row.get("username") or ""
        return out

    async def add_egg_log(self, entries: list) -> None:
        if entries:
            await self.egg_log.insert_many([dict(e) for e in entries])

    async def list_egg_log(self, user_id: int, kind: str = "", limit: int = 100,
                           before_ts: Optional[float] = None) -> list:
        query = {"user_id": user_id}
        if kind in ("eagle", "meat", "jackpot"):
            query["kind"] = kind
        if before_ts:
            query["ts"] = {"$lt": float(before_ts)}
        out = []
        async for doc in self.egg_log.find(query).sort("ts", -1).limit(limit):
            doc.pop("_id", None)
            out.append(doc)
        return out

    async def egg_log_summary(self, user_id: int) -> dict:
        """Итоги вскрытий игрока: {kind: {count, amount}} и время первой записи."""
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {"_id": "$kind", "count": {"$sum": 1}, "amount": {"$sum": "$amount"},
                        "first": {"$min": "$ts"}}},
        ]
        by_kind, first = {}, None
        async for row in self.egg_log.aggregate(pipeline):
            by_kind[row["_id"] or ""] = {"count": int(row["count"]), "amount": float(row.get("amount") or 0)}
            first = row["first"] if first is None else min(first, row["first"])
        return {"by_kind": by_kind, "first_ts": first}

    async def prune_egg_log(self, now: Optional[float] = None) -> int:
        cutoff = (time.time() if now is None else now) - EGG_LOG_RETENTION_SECONDS
        result = await self.egg_log.delete_many({"ts": {"$lt": cutoff}})
        return result.deleted_count

    async def ledger_summary(self, user_id: int) -> dict:
        """Итоги по источникам: {source: {coins: +/-, mnstr, gold, count}} и
        время первой записи (с какого момента ведётся журнал игрока)."""
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {"_id": "$source", "count": {"$sum": "$count"}, "first": {"$min": "$first_ts"},
                        **{f: {"$sum": f"$delta.{f}"} for f in LEDGER_FIELDS}}},
        ]
        by_source, first = {}, None
        async for row in self.balance_ledger.aggregate(pipeline):
            by_source[row["_id"] or ""] = {f: float(row.get(f) or 0) for f in LEDGER_FIELDS} | {"count": int(row["count"])}
            first = row["first"] if first is None else min(first, row["first"])
        return {"by_source": by_source, "first_ts": first}

    async def record_economy(self, amounts: dict, day: str) -> None:
        """Прибавляет amounts ({счётчик: число}) к общим и суточным счётчикам."""
        inc = {k: float(v) for k, v in amounts.items() if v}
        if not inc:
            return
        await self.economy.update_one({"_id": "total"}, {"$inc": inc}, upsert=True)
        await self.economy.update_one(
            {"_id": f"day:{day}"}, {"$inc": inc, "$setOnInsert": {"day": day}}, upsert=True,
        )

    async def get_economy(self, days: int = 14) -> dict:
        total = await self.economy.find_one({"_id": "total"}) or {}
        total.pop("_id", None)
        daily = []
        async for doc in self.economy.find({"_id": {"$regex": "^day:"}}).sort("day", -1).limit(days):
            doc.pop("_id", None)
            daily.append(doc)
        return {"total": total, "daily": daily}

    async def raise_clan_open_slots(self, minimum: int) -> int:
        """Поднимает число открытых мест до minimum у всех кланов, где их
        меньше (бесплатные стартовые места выросли) — остальное не трогает."""
        result = await self.clans.update_many({"open_slots": {"$lt": minimum}}, {"$set": {"open_slots": minimum}})
        return result.modified_count

    async def open_clan_slot(self, user_id: int, clan_id: str, price_gram: float, member_limit: int) -> str:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return "not_found"

        result = await self.clans.update_one(
            {"_id": oid, "leader_id": user_id, "open_slots": {"$lt": member_limit}},
            {"$inc": {"open_slots": 1}},
        )
        if result.modified_count == 0:
            clan = await self.clans.find_one({"_id": oid})
            if not clan:
                return "not_found"
            if clan.get("leader_id") != user_id:
                return "not_leader"
            return "slots_maxed"

        charge = await self.users.update_one(
            {"_id": user_id, "coins": {"$gte": price_gram}},
            {"$inc": {"coins": -price_gram, "ops": 1}},
        )
        if charge.modified_count == 0:
            await self.clans.update_one({"_id": oid}, {"$inc": {"open_slots": -1}})
            return "insufficient_funds"
        return "ok"

    async def burn_eagle_for_clan_power(self, user_id: int, monster_id: str,
                                         feed_levels: int, power: float) -> Optional[list]:
        """Сжигает первого попавшегося полностью прокачанного орла нужного
        вида с фермы (та же оптимистичная блокировка по monsters, что и в
        create_listing для орлов) и начисляет power очков ЛИЧНОГО
        burned_power сжёгшего игрока — одним атомарным update вместе со
        снятием орла с фермы. Клан здесь ни при чём: его сила теперь не
        хранится, а считается на лету суммой burned_power текущих
        участников (см. get_clan/list_top_clans), так что этот вклад
        учтётся автоматически, а при выходе из клана останется при
        игроке навсегда. Возвращает обновлённый список monsters или None,
        если такого орла нет."""
        doc = await self.users.find_one({"_id": user_id}, {"monsters": 1})
        farm = list((doc or {}).get("monsters") or [])
        # Орёл на рынке (замок) или в экспедиции не сжигается.
        idx = next((i for i, m in enumerate(farm)
                    if m.get("id") == monster_id and int(m.get("feed_level") or 0) >= feed_levels
                    and not m.get("listing_id") and not int(m.get("expedition_until") or 0)
                    and not int(m.get("hunt_until") or 0) and not m.get("raid_id")), None)
        if idx is None:
            return None
        original = farm[:]
        farm.pop(idx)
        result = await self.users.update_one(
            {"_id": user_id, "monsters": original},
            {"$set": {"monsters": farm}, "$inc": {"burned_power": power, "ops": 1}},
        )
        if result.modified_count == 0:
            return None
        return farm

    async def submit_clan_lineup(self, clan_id: str, user_id: int, tier_id: str) -> bool:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return False
        result = await self.clans.update_one(
            {"_id": oid, "members": user_id},
            {"$set": {f"lineup_submissions.{user_id}": {"tier_id": tier_id}}},
        )
        return result.modified_count > 0

    async def approve_clan_lineup(self, clan_id: str, leader_id: int, entries: list) -> bool:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return False
        result = await self.clans.update_one(
            {"_id": oid, "leader_id": leader_id},
            {"$set": {"approved_lineup": entries}},
        )
        return result.modified_count > 0

    # --- Турнирная сетка кланов, запускается ТОЛЬКО вручную админом (см.
    # /admin/api/clan_tournament/start и reconcile_clan_tournament в main.py) ---

    async def count_clans(self) -> int:
        """Сколько кланов сейчас на сервере (включая тестовых клан-ботов) — админ-панель
        показывает это число рядом с селектором масштаба турнира и
        блокирует запуск, если их меньше выбранного масштаба."""
        return await self.clans.count_documents({})

    async def get_clan_tournament(self) -> Optional[dict]:
        return await self.clan_tournament.find_one({"_id": "current"})

    async def try_launch_clan_tournament(self, bracket: list, size: int, start_at: float,
                                         match_minute: int = 20 * 60) -> str:
        """Админ вручную запускает турнир (кнопка «Утвердить состав и
        Запустить Турнир») — атомарно разрешает запуск только если
        предыдущего турнира нет вовсе, ИЛИ он уже полностью завершён
        (сыграны все матчи); иначе отказывает — "already_running".
        При повторном запуске 'cycle' — просто счётчик версий документа
        (нужен клиенту для подписи "Турнир №N"), не привязан к
        календарному циклу: турнир больше не запускается сам."""
        existing = await self.clan_tournament.find_one({"_id": "current"})
        if existing is None:
            await self.clan_tournament.update_one(
                {"_id": "current"},
                {"$setOnInsert": {"cycle": 0, "size": size, "start_at": start_at,
                                  "match_minute": match_minute, "bracket": bracket}},
                upsert=True,
            )
            fresh = await self.clan_tournament.find_one({"_id": "current"})
            return "ok" if fresh and fresh.get("start_at") == start_at and fresh.get("size") == size \
                else "already_running"

        if "size" not in existing:
            # Документ старого автоматического (календарного) турнира —
            # несовместим с ручной схемой, заменяется первым же запуском.
            result = await self.clan_tournament.update_one(
                {"_id": "current", "size": {"$exists": False}},
                {"$set": {"cycle": 0, "size": size, "start_at": start_at,
                          "match_minute": match_minute, "bracket": bracket}},
            )
            return "ok" if result.modified_count > 0 else "already_running"

        # Завершён — сыграны ВСЕ матчи (финал может быть перенесён админом
        # раньше матча за 3-е место, см. set_clan_match_start_override).
        if not (existing["bracket"] and all(m.get("resolved") for m in existing["bracket"])):
            return "already_running"

        prev_cycle = int(existing.get("cycle", 0))
        result = await self.clan_tournament.update_one(
            {"_id": "current", "cycle": prev_cycle, "bracket.resolved": {"$ne": False}},
            {"$set": {"cycle": prev_cycle + 1, "size": size, "start_at": start_at,
                      "match_minute": match_minute, "bracket": bracket}},
        )
        return "ok" if result.modified_count > 0 else "already_running"

    async def set_clan_tournament_match_minute(self, match_minute: int) -> bool:
        """Админ меняет время дня (минуты от полуночи UTC), в которое
        открываются матчи текущего турнира. False — турнир не запущен."""
        result = await self.clan_tournament.update_one(
            {"_id": "current", "size": {"$exists": True}},
            {"$set": {"match_minute": match_minute}},
        )
        return result.matched_count > 0

    async def set_clan_match_start_override(self, match_index: int, start_time: Optional[float]) -> bool:
        """Админ переносит один ещё не сыгранный матч на start_time (эпоха)
        или, при None, возвращает его к расписанию турнира. False — матча с
        таким индексом нет или он уже разрешён."""
        field = f"bracket.{match_index}.start_time_override"
        update = {"$set": {field: start_time}} if start_time is not None else {"$unset": {field: ""}}
        result = await self.clan_tournament.update_one(
            {"_id": "current", "size": {"$exists": True}, f"bracket.{match_index}.resolved": False},
            update,
        )
        return result.matched_count > 0

    async def cancel_clan_tournament(self) -> None:
        """Админ отменяет текущий турнир — документ удаляется целиком, после
        чего try_launch_clan_tournament видит «турнира нет» и разрешает новый
        запуск. Кланы и игроки не затрагиваются."""
        await self.clan_tournament.delete_one({"_id": "current"})

    async def resolve_clan_match(self, match_index: int, winner_id: Optional[str],
                                  winner_name: Optional[str], battle_log: list,
                                  resolved_at: Optional[float] = None,
                                  extra: Optional[dict] = None) -> bool:
        """Атомарно фиксирует исход одного матча — conditional update по
        индексу в массиве bracket, "resolved": False в фильтре гарантирует,
        что при гонке конкурентных запросов исход запишет только один из
        них. winner_id=None — техническая победа при пустом слоте (бай)."""
        result = await self.clan_tournament.update_one(
            {"_id": "current", f"bracket.{match_index}.resolved": False},
            {"$set": {
                f"bracket.{match_index}.resolved": True,
                f"bracket.{match_index}.winner_id": winner_id,
                f"bracket.{match_index}.winner_name": winner_name,
                f"bracket.{match_index}.battle_log": battle_log,
                f"bracket.{match_index}.resolved_at": resolved_at if resolved_at is not None else time.time(),
                **{f"bracket.{match_index}.{k}": v for k, v in (extra or {}).items()},
            }},
        )
        return result.modified_count > 0

    async def set_clan_match_participant(self, match_index: int, slot: str,
                                          clan_id: Optional[str], clan_name: Optional[str]) -> bool:
        """Проставляет победителя предыдущего раунда в слот следующего
        матча (slot — 'clan_a' или 'clan_b'), только если тот слот ещё
        пуст — так конкурентные запросы, разрешающие соседние матчи
        одновременно, не затирают друг друга."""
        result = await self.clan_tournament.update_one(
            {"_id": "current", f"bracket.{match_index}.{slot}_id": None},
            {"$set": {
                f"bracket.{match_index}.{slot}_id": clan_id,
                f"bracket.{match_index}.{slot}_name": clan_name,
            }},
        )
        return result.modified_count > 0

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

    # --- Админ: управление кланами (список/детали/модерация) ---

    def _clan_name_query(self, search: str) -> dict:
        search = (search or "").strip()
        return {"name": {"$regex": re.escape(search), "$options": "i"}} if search else {}

    async def list_clans_admin(self, search: str = "", limit: int = 50, offset: int = 0) -> tuple:
        """Список кланов для админ-панели с поиском по названию — сила
        клана считается на лету той же суммой burned_power участников,
        что и get_clan/list_top_clans."""
        query = self._clan_name_query(search)
        total = await self.clans.count_documents(query)
        out = []
        cursor = self.clans.find(query).sort("created_at", -1).skip(offset).limit(limit)
        async for doc in cursor:
            doc = dict(doc)
            doc["id"] = str(doc.pop("_id"))
            doc["clan_power"] = await self._sum_burned_power(doc.get("members") or [])
            out.append(doc)
        return out, total

    async def admin_rename_clan(self, clan_id: str, name: str) -> bool:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return False
        result = await self.clans.update_one({"_id": oid}, {"$set": {"name": name}})
        return result.matched_count > 0

    async def admin_set_clan_open_slots(self, clan_id: str, open_slots: int) -> bool:
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return False
        result = await self.clans.update_one({"_id": oid}, {"$set": {"open_slots": open_slots}})
        return result.matched_count > 0

    async def admin_disband_clan(self, clan_id: str) -> bool:
        """Админ распускает ЛЮБОЙ клан без проверки лидерства (в отличие
        от disband_clan, вызываемого самим лидером) — та же механика:
        клан удаляется, у всех участников снимается clan_id, их личный
        burned_power при этом не трогается."""
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return False
        clan = await self.clans.find_one_and_delete({"_id": oid})
        if not clan:
            return False
        members = clan.get("members") or []
        if members:
            await self.users.update_many({"_id": {"$in": members}}, {"$set": {"clan_id": None}})
        return True

    # --- Админ: тестовые клан-боты для обкатки турнирного пайплайна без
    # реальных игроков. is_bot=True на клане И на его фейковых участниках —
    # легко отличить от реальных данных и удалить одним действием. ---

    async def _alloc_bot_user_ids(self, count: int) -> list:
        """Атомарно выделяет count новых ID для тестовых ботов —
        ОТРИЦАТЕЛЬНЫЕ (реальные Telegram user_id всегда положительны).
        Счётчик counters.bot_user_id сдвигается одним $inc, поэтому два
        одновременных запроса (двойное нажатие «Создать массово») получают
        непересекающиеся блоки. $min перед этим опускает счётчик ниже уже
        существующих ботов (в т.ч. созданных до появления счётчика)."""
        from pymongo import ReturnDocument

        lowest = await self.users.find({"_id": {"$lt": 0}}, {"_id": 1}).sort("_id", 1).limit(1).to_list(1)
        floor = lowest[0]["_id"] if lowest else 0
        await self.counters.update_one({"_id": "bot_user_id"}, {"$min": {"value": floor}}, upsert=True)
        doc = await self.counters.find_one_and_update(
            {"_id": "bot_user_id"}, {"$inc": {"value": -count}}, return_document=ReturnDocument.AFTER,
        )
        end = int(doc["value"])
        return list(range(end + count - 1, end - 1, -1))

    @staticmethod
    def _bot_user_doc(uid: int, label: str, burned_power: float, clan_id: Optional[str],
                      loadout: Optional[dict] = None) -> dict:
        doc = {
            "_id": uid, "name": label, "is_bot": True,
            "coins": 0.0, "total_earned": 0.0, "mnstr": 0.0, "gold": 0.0,
            "monsters": [], "farm_queue": [], "active_slot": 0, "missions": [], "slots": 3,
            "referrals": 0, "referred_by": None, "last_seen": 0,
            "daily_day": 0, "daily_last": 0, "daily_cycles": 0,
            "eggs_board": [], "eggs_board_unlocked": 0, "eggs_queue": [], "wallet": "", "ops": 0,
            "vip_tier": "", "vip_expires_at": 0, "vip_last_meat_at": 0,
            "wheel_day": 0, "wheel_spins_today": 0,
            "nest_miners": [], "nest_particles": 0.0, "nest_last_claim": 0,
            "nest_inventory": {}, "nest_equipped": {},
            "pvp_rating": 1000, "pvp_energy": 0, "pvp_energy_day": 0,
            "clan_id": clan_id, "burned_power": burned_power,
        }
        if loadout:
            doc["monsters"] = [dict(loadout["farm_slot"])]
            doc["nest_equipped"] = {loadout["tier_id"]: dict(loadout["equipped"])}
        return doc

    async def create_bot_clan(self, name: str, clan_power: float, member_count: int,
                              loadouts: Optional[list] = None) -> str:
        """Создаёт ОДИН тестовый клан-бота с заданной силой — see
        clear_bot_clans для отката. Каждый бот — обычный документ users с
        отрицательным _id и is_bot=True; burned_power делится поровну между
        member_count ботами, так что clan_power клана (сумма burned_power
        его текущих участников — та же формула _sum_burned_power, что и у
        реальных кланов) равна запрошенной. Бот никогда не проходит
        authenticate() и не появляется в игре — это чистые данные для
        посева турнирной сетки и списков рейтинга. loadouts (см.
        bot_clan_loadouts в main.py) — орлы/снаряжение первых участников и
        утверждённая из них расстановка, записываются сразу при создании.
        Всего ~5 запросов к базе на клан (блок ID, клан, все боты разом)."""
        loadouts = list(loadouts or [])
        member_count = max(1, member_count, len(loadouts))
        share = clan_power / member_count
        member_ids = await self._alloc_bot_user_ids(member_count)
        lineup = [{"user_id": uid, "tier_id": lo["tier_id"]} for uid, lo in zip(member_ids, loadouts)]
        clan_doc = {
            "name": name, "leader_id": member_ids[0], "members": member_ids,
            "open_slots": member_count, "created_at": time.time(),
            "lineup_submissions": {str(e["user_id"]): {"tier_id": e["tier_id"]} for e in lineup},
            "approved_lineup": lineup, "applications": [],
            "is_bot": True,
        }
        result = await self.clans.insert_one(clan_doc)
        clan_id = str(result.inserted_id)
        await self.users.insert_many([
            self._bot_user_doc(
                uid, f"🤖 {name} #{i + 1}" if member_count > 1 else f"🤖 {name}", share, clan_id,
                loadouts[i] if i < len(loadouts) else None,
            )
            for i, uid in enumerate(member_ids)
        ])
        return clan_id

    async def equip_bot_clan(self, clan_id: str, loadouts: list) -> bool:
        """«Одевает» тестовый клан-бот для настоящего боя 10х10: при нехватке
        участников добирает ботов (с burned_power 0 — clan_power клана не
        меняется), кладёт i-му боту на ферму ровно одного орла из
        loadouts[i]["farm_slot"] со снаряжением loadouts[i]["equipped"] на
        эту редкость и записывает клану утверждённую расстановку
        (approved_lineup + lineup_submissions) — ту же, что утвердил бы
        лидер через /api/clan/lineup/approve. Повторный вызов просто
        перезаписывает всё тем же. Реальных кланов не касается (is_bot)."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(clan_id)
        except InvalidId:
            return False
        clan = await self.clans.find_one({"_id": oid, "is_bot": True})
        if not clan:
            return False
        members = list(clan.get("members") or [])
        name = clan.get("name") or "Bot Clan"
        missing = len(loadouts) - len(members)
        if missing > 0:
            new_ids = await self._alloc_bot_user_ids(missing)
            await self.users.insert_many([
                self._bot_user_doc(uid, f"🤖 {name} #{len(members) + i + 1}", 0.0, clan_id)
                for i, uid in enumerate(new_ids)
            ])
            members.extend(new_ids)

        lineup, submissions = [], {}
        for uid, loadout in zip(members, loadouts):
            tier_id = loadout["tier_id"]
            await self.users.update_one(
                {"_id": uid, "is_bot": True},
                {"$set": {"monsters": [loadout["farm_slot"]], "nest_equipped": {tier_id: loadout["equipped"]}}},
            )
            lineup.append({"user_id": uid, "tier_id": tier_id})
            submissions[str(uid)] = {"tier_id": tier_id}
        await self.clans.update_one({"_id": oid}, {"$set": {
            "members": members, "approved_lineup": lineup, "lineup_submissions": submissions,
            "open_slots": max(int(clan.get("open_slots") or 0), len(members)),
        }})
        return True

    async def list_bot_clans(self) -> list:
        out = []
        async for doc in self.clans.find({"is_bot": True}):
            doc = dict(doc)
            doc["id"] = str(doc.pop("_id"))
            doc["clan_power"] = await self._sum_burned_power(doc.get("members") or [])
            out.append(doc)
        return out

    async def clear_bot_clans(self) -> dict:
        """Удаляет ВСЕ тестовые клан-боты и всех фейковых ботов-пользователей
        разом — полный откат create_bot_clan. Ботов-пользователей удаляем по
        признаку (is_bot=True и отрицательный _id), а не только по составам
        кланов: исключённый из клана бот иначе оставался бы в базе навсегда.
        Реальных игроков не касается — у них _id = Telegram ID > 0 и нет is_bot."""
        clans = await self.clans.delete_many({"is_bot": True})
        users = await self.users.delete_many({"is_bot": True, "_id": {"$lt": 0}})
        return {"clans": clans.deleted_count, "users": users.deleted_count}

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

        try:
            oid = ObjectId(str(wd_id))
        except Exception:
            return False
        # Смена статуса и проверка «ещё pending» — одна атомарная операция:
        # раньше find + update шли раздельно, и двойной клик «Отклонить»
        # возвращал GRAM игроку дважды.
        doc = await self.withdrawals.find_one_and_update(
            {"_id": oid, "status": "pending"}, {"$set": {"status": status}},
        )
        if not doc:
            return False
        if refund:
            await self.users.update_one({"_id": doc["user_id"]}, {"$inc": {"coins": doc["amount"], "ops": 1}})
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
