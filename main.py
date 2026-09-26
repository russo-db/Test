import os
import json
import random
import re
import secrets
import time
import asyncio
from typing import List, Optional

import httpx

from auth import verify_init_data
from storage import make_store

from fastapi import FastAPI, Header, HTTPException, Request, Response, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "game_config.json")
EGG_BOARD_SIZE = 16  # 4×4
EGG_QUEUE_MAX = 300  # защитный предел на длину очереди яиц, не помещающихся на доску
# Клиент обновляет last_seen каждые 10с, пока приложение открыто (см. setInterval(saveNow, 10000)
# в index.html) — порог с запасом на сетевые задержки/уход в фон, чтобы игрок не мигал офлайн зря.
ONLINE_THRESHOLD_SECONDS = 60
FARM_QUEUE_MAX = 300  # защитный предел на длину очереди орлов, не помещающихся в открытые слоты
EAGLE_DELETE_MEAT_REWARD = 10  # фиксированная компенсация Meat за безвозвратное удаление орла
PVP_RATING_START = 1000  # стартовый PvP-рейтинг нового игрока для Топ-100 Арены
PVP_RATING_WIN = 25  # прирост рейтинга за победу на Арене
PVP_RATING_LOSS = 15  # потеря рейтинга за поражение на Арене (итог не опускается ниже 0)
ARENA_LADDER_ABOVE_COUNT = 5  # сколько ближайших мест НАД игроком учитывать при подборе соперника
ARENA_LADDER_RATING_RANGE = 100  # ± очков рейтинга для подбора «соседей» по месту в таблице
PVP_ENERGY_MAX = 10  # суточный потолок энергии Арены (донат может увести выше)
PVP_ENERGY_COST = 1  # энергии за один вход в бой
ARENA_ENERGY_PRICE_GOLD = 10  # золота за 1 докупленную энергию
ARENA_ENERGY_PRICE_GRAM = 0.25  # GRAM за 1 докупленную энергию
# ARENA_SEASON_DAYS/ARENA_SEASON_REWARDS — конфиг-драйвен, см. apply_config
# (game_config.json -> arena_season), чтобы игрок видел ту же таблицу наград
# в интерфейсе (CONFIG.arena_season.rewards), какую сервер реально выдаёт.

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
# Задавать вручную не обязательно: при старте бота имя берётся через getMe.
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@").strip()

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_config(cfg: dict):
    """Пересчитывает все производные от game_config.json глобальные переменные.
    Позволяет админ-панели менять баланс без перезапуска сервера."""
    global CONFIG, MISSIONS, MONSTERS, STARTER_MONSTER
    global START_SLOTS, MAX_SLOTS
    global FUSION_CFG, FEED_LEVELS, DAILY, DAILY_DAYS, DAILY_STEP, DAILY_SPECIAL
    global WHEEL, WHEEL_SEGMENTS, WHEEL_ENABLED, WHEEL_CHEAP_SPINS, WHEEL_CHEAP_COST, WHEEL_EXPENSIVE_COST
    global MISSIONS_ENABLED
    global TON, TON_RATE, MIN_DEPOSIT, MIN_WITHDRAW, MEMO_PREFIX, WITHDRAW_COMMISSION
    global MONSTER_TIER, TIER_INDEX, MARKET_CFG, MARKET_MIN_TIER_INDEX, MARKET_COMMISSION, MARKET_MIN_PRICE
    global EQUIP_MARKET_CFG, EQUIP_MARKET_COMMISSION, EQUIP_MARKET_MIN_PRICE
    global RESOURCE_MARKET_CFG, RESOURCE_MARKET_COMMISSION, RESOURCE_MARKET_MIN_PRICE, RESOURCE_MARKET_MIN_AMOUNT
    global ARENA_SEASON_CFG, ARENA_SEASON_DAYS, ARENA_SEASON_REWARDS
    global REFERRAL_SHARE, MAX_EGG_LEVEL
    global MAINTENANCE, MAINTENANCE_ENABLED, MAINTENANCE_MESSAGE, MAINTENANCE_CHAT_URL
    global EGGS_CFG, EGG_INTERVAL_HOURS, UNLOCK_PRICES
    global HATCH_COMMON_BY_LEVEL, HATCH_COMMON_DEFAULT, HATCH_MEAT_MIN, HATCH_MEAT_MAX
    global HATCH_JACKPOT_CHANCE, HATCH_JACKPOT_MEAT_BY_LEVEL
    global FEED_BASE_COST, FEED_GROWTH, FEED_TAPS_PER_LEVEL, MERGE_COST_MEAT, MERGE_COST_GRAM, ROULETTE_BY_TIER
    global EXPEDITIONS_CFG, EXPEDITION_DURATION_HOURS, EXPEDITION_COST_MEAT, EXPEDITION_GOLD_BY_TIER
    global SLOTS_PRICES, VIP_TIERS
    global NEST_CFG, NEST_SHARD_PRICE_GRAM, NEST_SHARD_COOLDOWN_HOURS, NEST_PARTICLE_INTERVAL_HOURS
    global NEST_CRAFT_COST_PARTICLES, NEST_CRAFT_COST_GOLD, NEST_UPGRADE_GROUP, NEST_GRADES, NEST_GRADE_BONUS
    global NEST_ITEM_TYPES, NEST_TYPE_ORDER, NEST_SHARD_DAILY_LIMIT
    global COMBAT_BASE_STATS, CLAN_CFG, CLAN_CREATE_COST_GRAM, CLAN_CREATE_COST_GOLD, CLAN_CREATE_COST_MEAT
    global CLAN_MEMBER_LIMIT, CLAN_INITIAL_OPEN_SLOTS, CLAN_SLOT_PRICE_GRAM, CLAN_BURN_POWER_BY_TIER
    global CLAN_TOURNAMENT_SIZE, CLAN_TOURNAMENT_DAYS, CLAN_ROSTER_SIZE

    CONFIG = cfg
    MISSIONS = {m["id"]: m for m in CONFIG["missions"]}
    REFERRAL_SHARE = float(CONFIG["referral"].get("share", 0.05))
    MONSTERS = {m["id"]: m for tier in CONFIG["tiers"] for m in tier["monsters"]}
    MONSTER_TIER = {m["id"]: tier["id"] for tier in CONFIG["tiers"] for m in tier["monsters"]}
    TIER_INDEX = {tier["id"]: i for i, tier in enumerate(CONFIG["tiers"])}
    STARTER_MONSTER = CONFIG["tiers"][0]["monsters"][0]["id"]
    MAX_EGG_LEVEL = len(CONFIG["tiers"])  # уровень яйца = редкость орла (1..N тиров, сейчас 6)
    START_SLOTS = CONFIG["slots"]["start"]
    MAX_SLOTS = CONFIG["slots"]["max"]
    FUSION_CFG = CONFIG.get("fusion") or {}
    FEED_LEVELS = int(FUSION_CFG.get("feed_levels", 7))
    FEED_BASE_COST = float(FUSION_CFG.get("feed_base_cost", 1))
    FEED_GROWTH = float(FUSION_CFG.get("feed_growth", 2))
    FEED_TAPS_PER_LEVEL = int(FUSION_CFG.get("feed_taps_per_level", 10))
    MERGE_COST_MEAT = float(FUSION_CFG.get("merge_cost_meat", 75))
    MERGE_COST_GRAM = float(FUSION_CFG.get("merge_cost_gram", 1))
    ROULETTE_BY_TIER = FUSION_CFG.get("roulette_by_tier") or []
    EGGS_CFG = CONFIG.get("eggs") or {}
    EGG_INTERVAL_HOURS = float(EGGS_CFG.get("egg_interval_hours", 24))
    UNLOCK_PRICES = [float(v) for v in (EGGS_CFG.get("unlock_prices") or [])]
    HATCH_COMMON_BY_LEVEL = [float(v) for v in (EGGS_CFG.get("hatch_common_chance_by_level") or [])]
    HATCH_COMMON_DEFAULT = float(EGGS_CFG.get("hatch_common_chance", 0.01))
    HATCH_MEAT_MIN = float(EGGS_CFG.get("hatch_meat_min", 7))
    HATCH_MEAT_MAX = float(EGGS_CFG.get("hatch_meat_max", 13))
    HATCH_JACKPOT_CHANCE = float(EGGS_CFG.get("hatch_jackpot_chance", 0.0))
    HATCH_JACKPOT_MEAT_BY_LEVEL = [float(v) for v in (EGGS_CFG.get("hatch_jackpot_meat_by_level") or [])]
    EXPEDITIONS_CFG = CONFIG.get("expeditions") or {}
    EXPEDITION_DURATION_HOURS = float(EXPEDITIONS_CFG.get("duration_hours", 4))
    EXPEDITION_COST_MEAT = float(EXPEDITIONS_CFG.get("cost_meat", 1))
    EXPEDITION_GOLD_BY_TIER = [float(v) for v in (EXPEDITIONS_CFG.get("gold_by_tier") or [])]
    SLOTS_PRICES = [float(v) for v in (CONFIG["slots"].get("prices") or [])]
    VIP_TIERS = {t["id"]: t for t in (CONFIG.get("vip") or {}).get("tiers", [])}
    MARKET_CFG = CONFIG.get("market") or {}
    MARKET_MIN_TIER_INDEX = 1  # обычная (индекс 0) редкость на P2P-рынке не продаётся
    MARKET_COMMISSION = float(MARKET_CFG.get("commission", 0.10))
    MARKET_MIN_PRICE = {k: float(v) for k, v in (MARKET_CFG.get("min_price_by_tier") or {}).items()}
    EQUIP_MARKET_CFG = CONFIG.get("equip_market") or {}
    EQUIP_MARKET_COMMISSION = float(EQUIP_MARKET_CFG.get("commission", 0.10))
    EQUIP_MARKET_MIN_PRICE = {k: float(v) for k, v in (EQUIP_MARKET_CFG.get("min_price_by_grade") or {}).items()}
    RESOURCE_MARKET_CFG = CONFIG.get("resource_market") or {}
    RESOURCE_MARKET_COMMISSION = float(RESOURCE_MARKET_CFG.get("commission", 0.10))
    RESOURCE_MARKET_MIN_PRICE = {k: float(v) for k, v in (RESOURCE_MARKET_CFG.get("min_price") or {}).items()}
    RESOURCE_MARKET_MIN_AMOUNT = {k: int(v) for k, v in (RESOURCE_MARKET_CFG.get("min_amount") or {}).items()}
    ARENA_SEASON_CFG = CONFIG.get("arena_season") or {}
    ARENA_SEASON_DAYS = int(ARENA_SEASON_CFG.get("days", 20))
    ARENA_SEASON_REWARDS = ARENA_SEASON_CFG.get("rewards") or [
        {"rank": 1, "gram": 50, "shards": 10, "particles": 0},
        {"rank": 2, "gram": 30, "shards": 4, "particles": 0},
        {"rank": 3, "gram": 20, "shards": 2, "particles": 0},
        {"rank": 4, "gram": 10, "shards": 1, "particles": 0},
        {"rank": 5, "gram": 3, "shards": 1, "particles": 0},
        {"rank_from": 6, "rank_to": 10, "gram": 0, "shards": 1, "particles": 0},
        {"rank_from": 11, "rank_to": 20, "gram": 0, "shards": 0, "particles": 10},
        {"rank_from": 21, "rank_to": 50, "gram": 0, "shards": 0, "particles": 1},
    ]
    DAILY = CONFIG.get("daily") or {}
    DAILY_DAYS = int(DAILY.get("days", 30))
    DAILY_STEP = float(DAILY.get("mnstr_step", 0.1))
    DAILY_SPECIAL = {int(item["day"]): item for item in DAILY.get("special", [])}

    WHEEL = CONFIG.get("wheel") or {}
    WHEEL_SEGMENTS = WHEEL.get("segments") or []
    WHEEL_ENABLED = bool(WHEEL.get("enabled", True))
    WHEEL_CHEAP_SPINS = int(WHEEL.get("cheap_spins", 3))
    WHEEL_CHEAP_COST = float(WHEEL.get("cheap_cost", 0.5))
    WHEEL_EXPENSIVE_COST = float(WHEEL.get("expensive_cost", 1))

    MISSIONS_ENABLED = bool(CONFIG.get("missions_enabled", True))

    TON = CONFIG.get("ton") or {}
    TON_RATE = float(TON.get("rate", 1))          # сколько GRAM даёт 1 TON
    MIN_DEPOSIT = float(TON.get("min_deposit", 1))
    MIN_WITHDRAW = float(TON.get("min_withdraw", 1))
    MEMO_PREFIX = str(TON.get("memo_prefix", "MG"))
    WITHDRAW_COMMISSION = float(TON.get("withdraw_commission", 0.10))

    MAINTENANCE = CONFIG.get("maintenance") or {}
    MAINTENANCE_ENABLED = bool(MAINTENANCE.get("enabled", False))
    MAINTENANCE_MESSAGE = str(MAINTENANCE.get("message") or "Ведутся технические работы. Скоро вернёмся!")
    MAINTENANCE_CHAT_URL = str(MAINTENANCE.get("chat_url") or "")

    # Гнездо Воинов: Небесные Осколки (пассивная добыча частичек), крафт и
    # улучшение снаряжения (4 одного грейда -> 1 следующего), экипировка
    # орлов по редкости (тир орла = "боевой орёл", один на редкость).
    NEST_CFG = CONFIG.get("nest") or {}
    NEST_SHARD_PRICE_GRAM = float(NEST_CFG.get("shard_price_gram", 3))
    NEST_SHARD_COOLDOWN_HOURS = float(NEST_CFG.get("shard_cooldown_hours", 6))
    NEST_SHARD_DAILY_LIMIT = int(NEST_CFG.get("shard_daily_limit", 4))
    NEST_PARTICLE_INTERVAL_HOURS = float(NEST_CFG.get("particle_interval_hours", 24))
    NEST_CRAFT_COST_PARTICLES = int(NEST_CFG.get("craft_cost_particles", 20))
    NEST_CRAFT_COST_GOLD = float(NEST_CFG.get("craft_cost_gold", 300))
    NEST_UPGRADE_GROUP = int(NEST_CFG.get("upgrade_group", 4))
    NEST_GRADES = list(NEST_CFG.get("grades") or ["grey", "green", "blue", "purple", "gold", "mythic"])
    NEST_GRADE_BONUS = {k: float(v) for k, v in (NEST_CFG.get("grade_bonus_pct") or {}).items()}
    NEST_ITEM_TYPES = NEST_CFG.get("item_types") or {}
    NEST_TYPE_ORDER = list(NEST_ITEM_TYPES.keys()) or ["claws", "armor", "mask", "ring"]

    # Кланы: боевые статы орлов (та же таблица, что и клиентский
    # NEST_EAGLE_BASE_STATS, теперь и на сервере — нужна для авторитетной
    # симуляции 10х10 «Прямого Эфира», см. combat_eagle_stats) и баланс
    # клана (стоимость создания, лимит участников, цена места, сила за
    # сожжённого 7-уровневого орла по редкости, размер турнирной сетки).
    COMBAT_BASE_STATS = CONFIG.get("combat_base_stats") or {}
    CLAN_CFG = CONFIG.get("clans") or {}
    CLAN_CREATE_COST_GRAM = float(CLAN_CFG.get("create_cost_gram", 10))
    CLAN_CREATE_COST_GOLD = float(CLAN_CFG.get("create_cost_gold", 500))
    CLAN_CREATE_COST_MEAT = float(CLAN_CFG.get("create_cost_meat", 1000))
    CLAN_MEMBER_LIMIT = int(CLAN_CFG.get("member_limit", 15))
    CLAN_INITIAL_OPEN_SLOTS = int(CLAN_CFG.get("initial_open_slots", 2))
    CLAN_SLOT_PRICE_GRAM = float(CLAN_CFG.get("slot_price_gram", 5))
    CLAN_BURN_POWER_BY_TIER = {k: float(v) for k, v in (CLAN_CFG.get("burn_power_by_tier") or {}).items()}
    CLAN_TOURNAMENT_SIZE = int(CLAN_CFG.get("tournament_size", 32))
    CLAN_TOURNAMENT_DAYS = int(CLAN_CFG.get("tournament_days", 24))
    CLAN_ROSTER_SIZE = int(CLAN_CFG.get("roster_size", 10))


apply_config(load_config())

# Кошелёк проекта — получатель пополнений. Без него раздел кошелька выключен.
TON_WALLET = os.getenv("TON_WALLET", "").strip()
TON_API = os.getenv("TON_API_URL", "https://toncenter.com/api/v3").rstrip("/")
TON_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
TON_POLL_SECONDS = int(os.getenv("TON_POLL_SECONDS", "30"))
# Куда слать заявки на вывод: свой Telegram-id или id канала.
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

# Пароль от админ-панели (/admin). Без него панель недоступна.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_TTL = 12 * 3600
ADMIN_SESSIONS: dict = {}  # token -> unix-время истечения


def admin_session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    expires = ADMIN_SESSIONS.get(token)
    if not expires or expires < time.time():
        ADMIN_SESSIONS.pop(token, None)
        return False
    return True


def require_admin(request: Request):
    if not admin_session_valid(request.cookies.get("admin_session")):
        raise HTTPException(status_code=401, detail="Не авторизован")


store = make_store()

# Подпись проверяется, как только известен токен бота. Без него (локальная
# разработка, статический хостинг) сервер работает в открытом режиме.
AUTH_REQUIRED = bool(BOT_TOKEN)


def authenticate(init_data: Optional[str], claimed_id: int) -> int:
    """Возвращает настоящий user_id из подписанных Telegram данных."""
    if not AUTH_REQUIRED:
        return claimed_id

    data = verify_init_data(init_data, BOT_TOKEN)
    if not data:
        raise HTTPException(status_code=401, detail="Некорректная подпись initData")
    if int(data["user"]["id"]) != int(claimed_id):
        raise HTTPException(status_code=403, detail="initData принадлежит другому пользователю")
    return int(data["user"]["id"])


def signed_context(init_data: Optional[str]) -> dict:
    """Подписанные поля запуска: имя игрока и реферальная нагрузка."""
    data = verify_init_data(init_data, BOT_TOKEN) if AUTH_REQUIRED else None
    if not data:
        return {}
    user = data["user"]
    parts = [user.get("first_name") or "", user.get("last_name") or ""]
    name = f"@{user['username']}" if user.get("username") else " ".join(p for p in parts if p).strip()
    return {"name": name or "Игрок", "start_param": data.get("start_param") or ""}


async def attach_referrer(user_id: int, name: str, start_param: str) -> bool:
    """Привязывает пригласившего при первом запуске игры.

    Ссылка вида ?startapp=<id> открывает Mini App напрямую, поэтому обработчик
    /start у бота не срабатывает — приглашение засчитываем здесь.
    """
    try:
        referrer = int(start_param)
    except (TypeError, ValueError):
        return False
    if referrer == user_id:
        return False

    created = await ensure_user(user_id, referrer, name)
    if not created:
        return False

    await ensure_user(referrer)
    await store.increment(referrer, {"referrals": 1})
    await notify_referrer(referrer, name)
    return True


async def inviter_name(referrer_id) -> str:
    """Имя пригласившего — чтобы игрок видел, чей он реферал."""
    if not referrer_id:
        return ""
    doc = await store.get(int(referrer_id))
    return (doc or {}).get("name") or ""


async def tg_send(chat_id, text: str):
    """Пишет в Telegram. Блокировка бота игроком не должна ронять запрос."""
    if not BOT_TOKEN or not chat_id:
        return False
    from telegram import Bot

    try:
        await Bot(BOT_TOKEN).send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
        )
        return True
    except Exception:
        return False


async def notify_referrer(referrer: int, friend_name: str):
    """Сообщает пригласившему, что друг пришёл."""
    pct = round(REFERRAL_SHARE * 100)
    await tg_send(
        referrer,
        f"🎉 К тебе присоединился <b>{friend_name}</b>!\n\n"
        f"Теперь ты будешь получать {pct}% GRAM с каждого его пополнения.",
    )


# --- GAME MATH (mirrored by the client in index.html) ---
def read_farm(raw) -> List[dict]:
    """The farm is one slot per eagle: {"id", "next_egg_at", "feed_level", "feed_taps",
    "expedition_until"}.

    Feeding is tap-driven: a fresh eagle starts at level 1. Every
    feed_taps_per_level taps starts a 24h egg timer (`next_egg_at`; 0 means no
    egg pending); collecting that egg is what advances feed_level by 1 (see
    collectEgg client-side). The max level (FEED_LEVELS) is terminal - the
    eagle no longer eats or lays eggs, only waits to be fused with another
    maxed eagle of the same kind into the next rarity. Farms saved in older
    shapes - {id: copies}, a flat list of ids, the very first {"id", "mined"}
    payout slots, or the lump-sum {"fed": bool} or leveled-by-taps schemes -
    are migrated; a previously fed/maxed eagle keeps that status, and any
    stray timer on an already-maxed eagle is cleared since max is terminal.
    """
    try:
        data = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []

    if isinstance(data, dict):
        data = [mid for mid, copies in data.items() for _ in range(int(copies or 0))]
    if not isinstance(data, list):
        return []

    farm = []
    for entry in data:
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            continue
        monster_id = entry.get("id")
        if monster_id not in MONSTERS:
            continue
        try:
            feed_level = int(entry["feed_level"])
        except (KeyError, TypeError, ValueError):
            feed_level = FEED_LEVELS if entry.get("fed") else 1
        feed_level = max(1, min(FEED_LEVELS, feed_level))
        try:
            feed_taps = max(0, int(entry["feed_taps"]))
        except (KeyError, TypeError, ValueError):
            feed_taps = 0
        try:
            next_egg_at = int(entry["next_egg_at"])
        except (KeyError, TypeError, ValueError):
            next_egg_at = 0
        if feed_level >= FEED_LEVELS:
            next_egg_at = 0
        try:
            expedition_until = int(entry["expedition_until"])
        except (KeyError, TypeError, ValueError):
            expedition_until = 0
        farm.append({
            "id": monster_id,
            "next_egg_at": next_egg_at,
            "feed_level": feed_level,
            "feed_taps": feed_taps,
            "expedition_until": max(0, expedition_until),
        })
    return farm


def normalize_eggs_board(raw) -> List[int]:
    """Доска — EGG_BOARD_SIZE ячеек (4×4). Приводит любые старые сохранения
    (доску 3×3 или 5×5) к текущему размеру. Если старая доска была больше и
    яйца лежали за пределами новой сетки, переносим их в свободные ячейки
    внутри неё вместо того, чтобы просто их терять. Уровень яйца сверху
    ограничен MAX_EGG_LEVEL — иначе подделанный /api/save мог бы протащить
    яйцо выше любого тира и вскрыть его на нереальную награду."""
    source = [min(MAX_EGG_LEVEL, max(0, int(v) if isinstance(v, (int, float)) else 0)) for v in raw] if isinstance(raw, list) else []
    board = source[:EGG_BOARD_SIZE]
    board += [0] * (EGG_BOARD_SIZE - len(board))
    for value in source[EGG_BOARD_SIZE:]:
        if not value:
            continue
        try:
            empty = board.index(0)
        except ValueError:
            break  # доска и так полна — дальше некуда
        board[empty] = value
    return board


def normalize_eggs_queue(raw) -> List[int]:
    """Очередь яиц, не поместившихся на доску, — список уровней по порядку
    (первое положенное — первое, что займёт освободившуюся ячейку). Уровень
    так же ограничен MAX_EGG_LEVEL, см. normalize_eggs_board."""
    source = [min(MAX_EGG_LEVEL, max(0, int(v) if isinstance(v, (int, float)) else 0)) for v in raw] if isinstance(raw, list) else []
    return [v for v in source if v][:EGG_QUEUE_MAX]


# --- FARM ACTIONS: game math ported from index.html so every server-side
# action produces exactly the same numbers the client used to compute itself.
# Every function here is pure (no I/O) — the calling endpoint reads the row,
# calls these to compute the new fields, then writes with store.cas_update().
def tier_index(monster_id: str) -> int:
    return TIER_INDEX.get(MONSTER_TIER.get(monster_id), 0)


def new_slot(monster_id: str) -> dict:
    return {"id": monster_id, "next_egg_at": 0, "feed_level": 1, "feed_taps": 0, "expedition_until": 0}


def feed_cost(monster_id: str) -> float:
    return FEED_BASE_COST * (FEED_GROWTH ** tier_index(monster_id))


def vip_active(row: dict) -> bool:
    tier = row.get("vip_tier")
    return bool(tier) and tier in VIP_TIERS and float(row.get("vip_expires_at") or 0) > time.time()


def current_vip_tier(row: dict) -> Optional[dict]:
    return VIP_TIERS.get(row.get("vip_tier")) if vip_active(row) else None


def egg_interval_seconds(row: dict) -> float:
    vip = current_vip_tier(row)
    base = EGG_INTERVAL_HOURS * 3600
    return max(0.0, base - (float(vip.get("egg_reduction_hours") or 0) * 3600 if vip else 0.0))


def expedition_duration_seconds(row: dict) -> float:
    vip = current_vip_tier(row)
    base = EXPEDITION_DURATION_HOURS * 3600
    return max(0.0, base - (float(vip.get("expedition_reduction_hours") or 0) * 3600 if vip else 0.0))


def expedition_gold_reward(monster_id: str) -> float:
    idx = tier_index(monster_id)
    return EXPEDITION_GOLD_BY_TIER[idx] if idx < len(EXPEDITION_GOLD_BY_TIER) else 0.0


def board_unlock_cost(unlocked_count: int) -> Optional[float]:
    """None — уже разблокировано всё, что есть в прайсе (защита от деления
    за пределами таблицы; ситуация не должна происходить при unlocked_count
    < EGG_BOARD_SIZE, но лучше явно отказать, чем открыть бесплатно)."""
    idx = unlocked_count - 2
    if 0 <= idx < len(UNLOCK_PRICES):
        return UNLOCK_PRICES[idx]
    return None


def pick_weighted_index(segments: List[dict]) -> int:
    total = sum(float(s.get("weight") or 0) for s in segments) or 1.0
    roll = random.uniform(0, total)
    upto = 0.0
    for i, seg in enumerate(segments):
        upto += float(seg.get("weight") or 0)
        if roll <= upto:
            return i
    return len(segments) - 1


def roll_monster(tier_id: str) -> str:
    tier = next((t for t in CONFIG["tiers"] if t["id"] == tier_id), CONFIG["tiers"][0])
    pool = tier["monsters"]
    total = sum(float(m.get("chance") or 0) for m in pool) or 1.0
    roll = random.uniform(0, total)
    upto = 0.0
    for m in pool:
        upto += float(m.get("chance") or 0)
        if roll <= upto:
            return m["id"]
    return pool[-1]["id"]


def roll_egg_outcome(level: int) -> dict:
    jackpot_meat = HATCH_JACKPOT_MEAT_BY_LEVEL[level - 1] if level - 1 < len(HATCH_JACKPOT_MEAT_BY_LEVEL) else 0.0
    jackpot_chance = HATCH_JACKPOT_CHANCE if jackpot_meat else 0.0
    common_chance = HATCH_COMMON_BY_LEVEL[level - 1] if level - 1 < len(HATCH_COMMON_BY_LEVEL) else HATCH_COMMON_DEFAULT
    roll = random.random()
    if roll < jackpot_chance:
        return {"kind": "jackpot", "amount": jackpot_meat}
    if roll < jackpot_chance + common_chance:
        return {"kind": "eagle"}
    base = HATCH_MEAT_MIN + random.random() * (HATCH_MEAT_MAX - HATCH_MEAT_MIN)
    return {"kind": "meat", "amount": round(base * (2 ** (level - 1)))}


def place_egg_on_board_or_queue(board: List[int], queue: List[int], unlocked: int, level: int):
    for i in range(unlocked):
        if not board[i]:
            board[i] = level
            return
    queue.append(level)


def drain_egg_queue(board: List[int], queue: List[int], unlocked: int):
    for i in range(unlocked):
        if not queue:
            break
        if not board[i]:
            board[i] = queue.pop(0)


def add_farm_slot(farm: List[dict], farm_queue: List[dict], slots_count: int, monster_id: str):
    slot = new_slot(monster_id)
    if len(farm) < slots_count:
        farm.append(slot)
    else:
        farm_queue.append(slot)


def drain_farm_queue(farm: List[dict], farm_queue: List[dict], slots_count: int):
    while len(farm) < slots_count and farm_queue:
        farm.append(farm_queue.pop(0))


async def grant_wheel_eggs(user_id: int, level: int, count: int) -> dict:
    """Кладёт count яиц уровня level на доску (или в очередь, если места нет)
    — приз колеса фортуны за яйца. Раньше доска яиц была чисто клиентским
    состоянием и это делал клиент сам, локально; теперь доска — серверное
    состояние (см. /api/eggs/*), поэтому и этот приз кладёт сервер."""
    board, queue = [], []
    for _ in range(3):
        row = await fetch_user(user_id)
        ops = int(row.get("ops") or 0)
        board = normalize_eggs_board(row.get("eggs_board"))
        queue = normalize_eggs_queue(row.get("eggs_queue"))
        unlocked = max(2, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 2)))
        board_count = 0
        queue_count = 0
        for _ in range(count):
            placed = False
            for i in range(unlocked):
                if not board[i]:
                    board[i] = level
                    placed = True
                    break
            if placed:
                board_count += 1
            else:
                queue.append(level)
                queue_count += 1
        if await store.cas_update(user_id, {"eggs_board": board, "eggs_queue": queue}, ops):
            return {"board": board_count, "queue": queue_count, "eggs_board": board, "eggs_queue": queue}
    return {"board": 0, "queue": 0, "eggs_board": board, "eggs_queue": queue}


async def apply_wheel_reward(user_id: int, reward: dict) -> dict:
    """Начисляет GRAM/Meat приза колеса сразу; орла-приз всегда кладёт в
    очередь (farm_queue) — не в открытый слот фермы, даже если там есть
    место. Игрок сам разбирает очередь по ходу игры (покупка слота,
    удачное слияние и т.д.). Бесплатный слот сверх купленных не выдаётся —
    раньше при полной ферме на максимуме слотов приз-орёл вообще терялся
    (запрос отклонялся с 400 уже ПОСЛЕ списания стоимости прокрута)."""
    for _ in range(5):
        row = await fetch_user(user_id)
        ops = int(row.get("ops") or 0)
        coins = float(row.get("coins") or 0) + reward["gram"]
        total_earned = float(row.get("total_earned") or 0) + reward["gram"]
        mnstr = float(row.get("mnstr") or 0) + reward["mnstr"]
        fields = {"coins": coins, "total_earned": total_earned, "mnstr": mnstr}

        if reward["monster"]:
            farm_queue = read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX]
            farm_queue.append(new_slot(reward["monster"]))
            fields["farm_queue"] = farm_queue[:FARM_QUEUE_MAX]

        if await store.cas_update(user_id, fields, ops):
            fresh = dict(row)
            fresh.update(fields)
            fresh["ops"] = ops + 1
            return fresh
    raise HTTPException(status_code=409, detail="Не удалось начислить приз — попробуй ещё раз")


async def reconcile_queues(user_id: int, row: dict) -> dict:
    """Разбирает очередь орлов и очередь яиц при каждой загрузке фермы: если
    в открытых слотах или на разблокированных ячейках доски есть место, а
    в очереди кто-то ждёт — сразу переносит и сохраняет. Раньше это делал
    только клиент, локально и молча — до следующей загрузки перенос
    существовал лишь на экране и никогда не попадал на сервер, поэтому
    "переехавший" орёл/яйцо откатывались назад и переставали открываться."""
    for _ in range(3):
        farm = read_farm(row.get("monsters"))
        farm_queue = read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX]
        slots_count = int(row.get("slots") or START_SLOTS)
        farm_len_before = len(farm)
        drain_farm_queue(farm, farm_queue, slots_count)
        farm_changed = len(farm) != farm_len_before

        board = normalize_eggs_board(row.get("eggs_board"))
        queue = normalize_eggs_queue(row.get("eggs_queue"))
        unlocked = max(2, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 2)))
        board_before = board[:]
        drain_egg_queue(board, queue, unlocked)
        board_changed = board != board_before

        if not farm_changed and not board_changed:
            return row

        ops = int(row.get("ops") or 0)
        fields = {"monsters": farm, "farm_queue": farm_queue, "eggs_board": board, "eggs_queue": queue}
        if await store.cas_update(user_id, fields, ops):
            row = dict(row)
            row.update(fields)
            row["ops"] = ops + 1
            return row
        row = await fetch_user(user_id)  # гонка с другим действием — перечитать и попробовать снова
    return row


# --- ГНЕЗДО ВОИНОВ: Небесные Осколки, добыча частичек, крафт/улучшение
# снаряжения, экипировка. Боевой орёл один на редкость (тир = "боевой
# орёл", как и вид орла на ферме) — экипировка не привязана к конкретному
# слоту фермы, который может быть продан/слит/удалён. ---

def nest_empty_inventory() -> dict:
    return {t: {g: 0 for g in NEST_GRADES} for t in NEST_TYPE_ORDER}


def normalize_nest_inventory(raw) -> dict:
    inventory = nest_empty_inventory()
    if isinstance(raw, dict):
        for item_type in NEST_TYPE_ORDER:
            sub = raw.get(item_type)
            if not isinstance(sub, dict):
                continue
            for grade in NEST_GRADES:
                try:
                    inventory[item_type][grade] = max(0, int(sub.get(grade) or 0))
                except (TypeError, ValueError):
                    pass
    return inventory


def pvp_rating_of(row: dict) -> int:
    """PvP-рейтинг игрока — у аккаунтов, созданных до Арены, поля ещё нет,
    поэтому отсутствующее значение (не 0 — 0 сам по себе законный итог
    после серии поражений) трактуется как стартовые PVP_RATING_START."""
    value = row.get("pvp_rating")
    return PVP_RATING_START if value is None else max(0, int(value))


def pvp_energy_of(row: dict):
    """(энергия, day_index последнего пополнения) с учётом суточного
    пополнения до PVP_ENERGY_MAX по UTC-суткам (см. day_index) — только
    ПОДНИМАЕТ энергию в новые сутки, никогда не отнимает задонатенное
    сверх потолка. Чистая функция, ничего не пишет в БД сама (см.
    reconcile_pvp_energy для персистентного пополнения на /api/load и
    arena_spend_energy/arena_buy_energy для трат)."""
    energy = row.get("pvp_energy")
    energy = float(PVP_ENERGY_MAX) if energy is None else float(energy)
    last_day = row.get("pvp_energy_day")
    today = day_index()
    if last_day is None or int(last_day) < today:
        energy = max(energy, PVP_ENERGY_MAX)
        last_day = today
    else:
        last_day = int(last_day)
    return energy, last_day


def arena_energy_reset_at(day: int) -> float:
    """Момент следующего суточного пополнения энергии — начало следующих
    UTC-суток после day (см. day_index)."""
    return (day + 1) * 86400


async def reconcile_pvp_energy(user_id: int, row: dict) -> dict:
    """Персистентно пополняет pvp_energy, если наступили новые UTC-сутки с
    последнего пополнения — вызывается из /api/load, как и accrue_vip_meat,
    чтобы обновление происходило само по факту захода в игру, без отдельного
    действия игрока."""
    energy, day = pvp_energy_of(row)
    if row.get("pvp_energy") == energy and row.get("pvp_energy_day") == day:
        return row  # уже актуально, писать нечего

    for _ in range(3):
        ops = int(row.get("ops") or 0)
        fields = {"pvp_energy": energy, "pvp_energy_day": day}
        if await store.cas_update(user_id, fields, ops):
            row = dict(row)
            row.update(fields)
            row["ops"] = ops + 1
            return row
        row = await fetch_user(user_id)  # гонка с другим действием — перечитать и попробовать снова
        energy, day = pvp_energy_of(row)
    return row


def best_owned_tier(monsters_raw) -> str:
    """Редкость самого высокоуровневого орла в коллекции — значок для
    Таблицы лидеров Арены. TIER_INDEX растёт от обычного к мифическому,
    поэтому достаточно максимума по индексу среди реально имеющихся тиров."""
    farm = read_farm(monsters_raw)
    tiers = {MONSTER_TIER.get(slot.get("id")) for slot in farm}
    tiers.discard(None)
    if not tiers:
        return CONFIG["tiers"][0]["id"]
    return max(tiers, key=lambda tier_id: TIER_INDEX.get(tier_id, 0))


def normalize_nest_equipped(raw) -> dict:
    equipped = {tier_id: {t: None for t in NEST_TYPE_ORDER} for tier_id in TIER_INDEX}
    if isinstance(raw, dict):
        for tier_id in TIER_INDEX:
            sub = raw.get(tier_id)
            if not isinstance(sub, dict):
                continue
            for item_type in NEST_TYPE_ORDER:
                grade = sub.get(item_type)
                if grade in NEST_GRADES:
                    equipped[tier_id][item_type] = grade
    return equipped


def normalize_nest_miners(raw) -> List[dict]:
    """Осколки в Кузнице теперь лишь считаются (см. nest_owned_shards) — у
    накопления частичек больше нет персонального таймера на каждый осколок
    (см. nest_pending_particles), поэтому храним только id."""
    miners = []
    if isinstance(raw, list):
        for m in raw:
            if isinstance(m, dict) and m.get("id"):
                miners.append({"id": str(m["id"])})
    return miners


def nest_owned_shards(row: dict) -> int:
    return len(normalize_nest_miners(row.get("nest_miners")))


def nest_particle_rate_per_second(owned_shards: int) -> float:
    """1 осколок = 1 частичка за NEST_PARTICLE_INTERVAL_HOURS часов, поровну
    размазанная по секундам — суммарная ставка растёт линейно с числом
    осколков (см. nest_pending_particles)."""
    interval = NEST_PARTICLE_INTERVAL_HOURS * 3600
    if interval <= 0 or owned_shards <= 0:
        return 0.0
    return owned_shards / interval


def nest_last_claim_of(row: dict, now: float) -> float:
    """Точка отсчёта накопления. Для новых игроков её сразу проставляет
    ensure_user; для тех, кто играл ДО перехода на эту формулу (там не было
    last_claim, был счёт по каждому осколку отдельно — last_collect_at),
    один раз мигрируем на самый старый last_collect_at среди их осколков,
    чтобы честно недобранный по старой системе прогресс не сгорел, а
    досчитался уже по новой формуле (см. reconcile_nest_particles)."""
    stored = row.get("nest_last_claim")
    if stored is not None:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    raw_miners = row.get("nest_miners")
    if isinstance(raw_miners, list) and raw_miners:
        legacy_times = [
            float(m["last_collect_at"]) for m in raw_miners
            if isinstance(m, dict) and m.get("last_collect_at")
        ]
        if legacy_times:
            return min(legacy_times)
    return now


def nest_pending_particles(row: dict, now: float) -> float:
    """Сколько частичек накопилось ПРЯМО СЕЙЧАС, непрерывно и с дробной
    частью: secondsPassed * (owned_shards / interval). last_claim не
    двигается сам по себе — только через явный сбор (nest_particles_collect)
    или пересчёт ставки при смене числа осколков (nest_settle_particles)."""
    rate = nest_particle_rate_per_second(nest_owned_shards(row))
    if rate <= 0:
        return 0.0
    last_claim = nest_last_claim_of(row, now)
    return max(0.0, now - last_claim) * rate


def nest_settle_particles(row: dict, now: float, new_owned_shards: int) -> float:
    """Вызывать СТРОГО ДО того, как число осколков в row реально изменится
    (например, перед покупкой нового) — пересчитывает last_claim под новую
    ставку так, чтобы уже накопленный (но ещё не собранный) дробный прогресс
    остался тем же самым числом частичек, а не потерялся и не задвоился
    из-за смены знаменателя формулы."""
    pending = nest_pending_particles(row, now)
    new_rate = nest_particle_rate_per_second(new_owned_shards)
    if pending <= 0 or new_rate <= 0:
        return now
    return now - (pending / new_rate)


async def reconcile_nest_particles(user_id: int, row: dict) -> dict:
    """Одноразовая ленивая миграция на last_claim для игроков, у которых
    его ещё нет (см. nest_last_claim_of) — вызывается из /api/load, как и
    accrue_vip_meat/reconcile_pvp_energy. Сама ничего не начисляет, только
    фиксирует точку отсчёта; дальше копится и собирается через
    /api/nest/particles/collect."""
    if row.get("nest_last_claim") is not None:
        return row
    now = time.time()
    last_claim = nest_last_claim_of(row, now)
    for _ in range(3):
        ops = int(row.get("ops") or 0)
        if await store.cas_update(user_id, {"nest_last_claim": last_claim}, ops):
            row = dict(row)
            row["nest_last_claim"] = last_claim
            row["ops"] = ops + 1
            return row
        row = await fetch_user(user_id)
        if row.get("nest_last_claim") is not None:
            return row
    return row


def nest_next_grade(grade: str) -> Optional[str]:
    i = NEST_GRADES.index(grade) if grade in NEST_GRADES else -1
    return NEST_GRADES[i + 1] if 0 <= i < len(NEST_GRADES) - 1 else None


async def nest_state_view(row: dict) -> dict:
    """Состояние Гнезда Воинов для /api/load — то же, что возвращают и все
    /api/nest/* эндпоинты, чтобы клиент обновлял его одинаково что после
    загрузки, что после действия. Кулдаун и суточный лимит покупки
    Небесного Осколка — общий (не персональный) ресурс на всех игроков
    сразу, см. store.buy_nest_shard/get_nest_state; частички, инвентарь и
    экипировка остаются личными для каждого игрока."""
    shard = await store.get_nest_state(day_index(), NEST_SHARD_DAILY_LIMIT)
    now = time.time()
    return {
        "shard_price_gram": NEST_SHARD_PRICE_GRAM,
        "shard_cooldown_until": shard["cooldown_until"],
        "shard_bought_today": shard["bought_today"],
        "shard_daily_limit": shard["daily_limit"],
        "particles": float(row.get("nest_particles") or 0),
        "shard_count": nest_owned_shards(row),
        "last_claim": nest_last_claim_of(row, now),
        "inventory": normalize_nest_inventory(row.get("nest_inventory")),
        "equipped": normalize_nest_equipped(row.get("nest_equipped")),
    }


async def accrue_vip_meat(user_id: int, row: dict) -> dict:
    """Начисляет накопленный VIP-Meat (раз в сутки, с наверстыванием за
    время офлайн, но не дольше, чем тариф был активен) — раньше это делал
    клиент в collectVipDailyMeat() при каждом заходе. Вызывается из
    /api/load, чтобы офлайн-время не пропадало зря."""
    if not vip_active(row):
        return row
    tier = current_vip_tier(row)
    if not tier or not tier.get("meat_per_day"):
        return row
    now = time.time()
    day_len = 86400
    last = float(row.get("vip_last_meat_at") or 0)
    days = int((now - last) // day_len)
    if days <= 0:
        return row
    cap_at = min(now, float(row.get("vip_expires_at") or 0))
    days = min(days, max(0, int((cap_at - last) // day_len)))
    if days <= 0:
        return row

    for _ in range(3):
        ops = int(row.get("ops") or 0)
        new_last = last + days * day_len
        amount = days * float(tier["meat_per_day"])
        mnstr = float(row.get("mnstr") or 0) + amount
        fields = {"vip_last_meat_at": new_last, "mnstr": mnstr}
        if await store.cas_update(user_id, fields, ops):
            row = dict(row)
            row.update(fields)
            row["ops"] = ops + 1
            return row
        row = await fetch_user(user_id)  # гонка с другим действием — перечитать и попробовать снова
    return row


async def run_farm_action(user_id: int, compute) -> dict:
    """Общий цикл для всех действий фермы/яиц/VIP: читает документ, зовёт
    compute(row) -> (fields для $set, доп. поля ответа) и пишет через
    cas_update по прочитанному ops. compute может кинуть HTTPException —
    тогда действие отклоняется без записи. Конфликт (параллельное действие
    того же игрока сдвинуло ops между чтением и записью) — не ошибка
    игрока, а гонка тапов; перечитываем документ и пробуем снова."""
    for _ in range(5):
        row = await fetch_user(user_id)
        ops = int(row.get("ops") or 0)
        fields, extra = compute(row)
        if await store.cas_update(user_id, fields, ops):
            extra["status"] = "success"
            extra["ops"] = ops + 1
            return extra
    raise HTTPException(status_code=409, detail="Не удалось выполнить — попробуй ещё раз")


# --- DAILY CHECK-IN (mirrored by the client in index.html) ---
def day_index(moment: Optional[float] = None) -> int:
    """Порядковый номер суток UTC — по нему считаем серию входов."""
    return int((moment if moment is not None else time.time()) // 86400)


# --- СЕЗОНЫ АРЕНЫ: раз в ARENA_SEASON_DAYS дней Топ-50 получает призы
# (см. distribute_arena_rewards), после чего PvP-рейтинг сбрасывается
# всем игрокам разом — см. reconcile_arena_season. ---
def arena_season_index(moment: Optional[float] = None) -> int:
    """Порядковый номер сезона Арены — тот же принцип, что и day_index,
    но с шагом ARENA_SEASON_DAYS суток."""
    return int((moment if moment is not None else time.time()) // (ARENA_SEASON_DAYS * 86400))


def arena_season_ends_at(season: int) -> float:
    return (season + 1) * ARENA_SEASON_DAYS * 86400


def arena_season_view() -> dict:
    """Текущий сезон Арены для клиента — чистая функция от времени, не
    требует чтения БД (в отличие от reconcile_arena_season, который решает,
    не пора ли уже провести смену сезона)."""
    season = arena_season_index()
    return {"season": season, "ends_at": arena_season_ends_at(season)}


def daily_reward(day: int, first_lap: bool = True) -> dict:
    """Награда за day-й день серии: Meat по нарастающей (day × шаг), кроме особых дней.

    После первого прохождения круга (first_lap=False) особые дни, у которых
    задан repeat_mnstr, выдают вместо своей обычной награды (например, орла)
    просто Meat в этом количестве — так круг можно проходить бесконечно."""
    special = DAILY_SPECIAL.get(day)
    if special:
        if not first_lap and special.get("repeat_mnstr") is not None:
            return {"gram": 0.0, "mnstr": float(special.get("repeat_mnstr") or 0.0), "monster": None}
        return {
            "gram": float(special.get("gram") or 0.0),
            "mnstr": float(special.get("mnstr") or 0.0),
            "monster": special.get("monster"),
        }
    return {"gram": 0.0, "mnstr": round(day * DAILY_STEP, 2), "monster": None}


def daily_state(row: dict) -> dict:
    """Что показать игроку: сколько дней подряд забрано и доступен ли сегодняшний."""
    today = day_index()
    last = int(row.get("daily_last") or 0)
    streak = int(row.get("daily_day") or 0)
    if last != today and last != today - 1:
        streak = 0            # пропущенный день обнуляет серию
    if streak >= DAILY_DAYS and last != today:
        streak = 0            # круг пройден — начинаем заново
    return {
        "days": DAILY_DAYS,
        "last": last,
        "claimed": streak,
        "day": min(streak + 1, DAILY_DAYS) if last != today else streak,
        "ready": last != today,
        "first_lap": int(row.get("daily_cycles") or 0) == 0,
    }


# --- КОЛЕСО ФОРТУНЫ ---
def wheel_state(row: dict) -> dict:
    """Что показать игроку: сколько прокрутов уже сделано сегодня и сколько
    будет стоить следующий (первые wheel.cheap_spins в сутки — дешевле)."""
    today = day_index()
    stored_day = int(row.get("wheel_day") or 0)
    spins_today = int(row.get("wheel_spins_today") or 0) if stored_day == today else 0
    next_cost = WHEEL_CHEAP_COST if spins_today < WHEEL_CHEAP_SPINS else WHEEL_EXPENSIVE_COST
    return {
        "spins_today": spins_today,
        "cheap_spins": WHEEL_CHEAP_SPINS,
        "cheap_cost": WHEEL_CHEAP_COST,
        "expensive_cost": WHEEL_EXPENSIVE_COST,
        "next_cost": next_cost,
    }


def _wheel_reward(index: int) -> dict:
    """gram/mnstr/monster сервер начисляет сам. egg_level/egg_count — не
    деньги и не орёл: это яйца, которые нужно положить на доску яиц, а её
    состояние (eggs_board) целиком клиентское (как и вскрытие яиц вручную),
    поэтому сервер их не резолвит — просто передаёт клиенту, чтобы он сам
    разложил их по свободным ячейкам и сохранил."""
    seg = WHEEL_SEGMENTS[index]
    return {
        "index": index,
        "gram": float(seg.get("gram") or 0),
        "mnstr": float(seg.get("mnstr") or 0),
        "monster": seg.get("monster"),
        "egg_level": seg.get("egg_level"),
        "egg_count": seg.get("egg_count"),
    }


def wheel_pick() -> dict:
    """Взвешенный случайный сектор. Индекс нужен клиенту, чтобы анимация
    останавливалась ровно на секторе, который выбрал сервер."""
    total = sum(float(seg.get("weight", 1)) for seg in WHEEL_SEGMENTS) or 1
    roll = random.uniform(0, total)
    upto = 0.0
    for index, seg in enumerate(WHEEL_SEGMENTS):
        upto += float(seg.get("weight", 1))
        if roll <= upto:
            return _wheel_reward(index)
    return _wheel_reward(len(WHEEL_SEGMENTS) - 1)


async def ensure_user(user_id: int, referred_by: Optional[int] = None,
                      name: str = "") -> bool:
    """Создаёт игрока, если его ещё нет. True — если создан только что."""
    return await store.create(
        {
            "user_id": user_id,
            "name": name,
            "coins": 0.0,
            "total_earned": 0.0,
            "mnstr": 10.0,
            "gold": 0.0,
            "monsters": [{"id": STARTER_MONSTER, "next_egg_at": 0, "feed_level": 1, "feed_taps": 0}],
            "farm_queue": [],
            "active_slot": 0,
            "missions": [],
            "slots": START_SLOTS,
            "referrals": 0,
            "referred_by": referred_by,
            "last_seen": int(time.time()),
            "daily_day": 0,
            "daily_last": 0,
            "daily_cycles": 0,
            "eggs_board": [0] * EGG_BOARD_SIZE,
            "eggs_board_unlocked": 2,
            "eggs_queue": [],
            "wallet": "",
            "ops": 0,
            "vip_tier": "",
            "vip_expires_at": 0,
            "vip_last_meat_at": 0,
            "wheel_day": 0,
            "wheel_spins_today": 0,
            "nest_miners": [],
            "nest_particles": 0.0,
            "nest_last_claim": time.time(),
            "nest_inventory": {},
            "nest_equipped": {},
            "pvp_rating": PVP_RATING_START,
            "pvp_energy": PVP_ENERGY_MAX,
            "pvp_energy_day": day_index(),
            "clan_id": None,
            "burned_power": 0.0,
        }
    )


async def fetch_user(user_id: int) -> dict:
    await ensure_user(user_id)
    return await store.get(user_id)


# --- TON WALLET ---
def deposit_memo(user_id: int) -> str:
    """Мемо пополнения. По нему сервер понимает, чей это перевод."""
    return f"{MEMO_PREFIX}{user_id}"


def memo_user(comment: Optional[str]) -> Optional[int]:
    comment = (comment or "").strip()
    if not comment.upper().startswith(MEMO_PREFIX.upper()):
        return None
    tail = comment[len(MEMO_PREFIX):].strip()
    return int(tail) if tail.isdigit() else None


def valid_address(address: str) -> bool:
    """Дружественный вид (EQ…/UQ…, 48 символов) или сырой 0:hex."""
    address = (address or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{48}", address):
        return True
    return bool(re.fullmatch(r"-?\d+:[0-9a-fA-F]{64}", address))


def ton_info(user_id: int) -> dict:
    return {
        "enabled": bool(TON_WALLET),
        "wallet": TON_WALLET,
        "memo": deposit_memo(user_id),
        "rate": TON_RATE,
        "min_deposit": MIN_DEPOSIT,
        "min_withdraw": MIN_WITHDRAW,
        "withdraw_commission": WITHDRAW_COMMISSION,
    }


async def fetch_incoming() -> List[dict]:
    """Последние входящие переводы на кошелёк проекта с разобранным мемо."""
    if not TON_WALLET:
        return []

    headers = {"X-API-Key": TON_API_KEY} if TON_API_KEY else {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{TON_API}/transactions",
            params={"account": TON_WALLET, "limit": 100, "sort": "desc"},
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

    incoming = []
    for tx in payload.get("transactions") or []:
        message = tx.get("in_msg") or {}
        if not message.get("source"):
            continue                      # внешнее сообщение, а не перевод
        decoded = (message.get("message_content") or {}).get("decoded") or {}
        user_id = memo_user(decoded.get("comment"))
        if not user_id:
            continue
        try:
            nano = int(message.get("value") or 0)
        except (TypeError, ValueError):
            continue
        tx_hash = tx.get("hash") or message.get("hash")
        if nano <= 0 or not tx_hash:
            continue
        incoming.append({
            "hash": tx_hash,
            "user_id": user_id,
            "ton": nano / 1e9,
            "ts": int(tx.get("now") or time.time()),
        })
    return incoming


_last_scan = 0.0
_scan_lock = asyncio.Lock()


async def credit_deposits(force: bool = False) -> int:
    """Зачисляет все пополнения, которых ещё нет в базе. Повторы отсекает хэш.

    У публичного toncenter жёсткий лимит запросов, поэтому проверки идут по
    одной: фоновая молчит, если только что уже сканировали, а нажатие кнопки
    (force) ждёт своей очереди, но не чаще одного запроса в секунду.
    """
    global _last_scan

    async with _scan_lock:
        if not force and time.time() - _last_scan < 5:
            return 0
        pause = 1.0 - (time.time() - _last_scan)
        if pause > 0:
            await asyncio.sleep(pause)
        _last_scan = time.time()
        return await _scan_deposits()


async def _scan_deposits() -> int:
    try:
        incoming = await fetch_incoming()
    except Exception as error:
        print(f"TON: не удалось получить транзакции: {error}")
        return 0

    credited = 0
    for item in incoming:
        gram = round(item["ton"] * TON_RATE, 9)
        if gram <= 0:
            continue
        await ensure_user(item["user_id"])
        row = await store.get(item["user_id"])
        referrer_id = (row or {}).get("referred_by")
        referral_gram = round(gram * REFERRAL_SHARE, 9) if referrer_id else 0.0
        if await store.credit_deposit(
            item["hash"], item["user_id"], gram, item["ts"], referrer_id, referral_gram,
        ):
            credited += 1
            await tg_send(
                item["user_id"],
                f"✅ Пополнение зачислено: <b>{gram:g} GRAM</b> "
                f"({item['ton']:g} TON).",
            )
            if referrer_id and referral_gram > 0:
                await tg_send(
                    referrer_id,
                    f"🤝 Реферальный бонус: <b>{referral_gram:g} GRAM</b> "
                    f"— друг пополнил баланс.",
                )
    return credited


async def ton_poller():
    """Фоновая проверка пополнений по мемо — игроку ничего нажимать не нужно."""
    while True:
        await credit_deposits()
        await asyncio.sleep(TON_POLL_SECONDS)


# --- API MODELS ---
class FarmState(BaseModel):
    """Раньше сюда приходило целиком посчитанное клиентом состояние фермы —
    теперь каждое действие (кормление, сбор/вскрытие яйца, слияние,
    экспедиция, покупка слота/ячейки/VIP) атомарно считает и пишет сервер
    сам, см. /api/farm/*, /api/eggs/*, /api/vip/*. Экономических полей тут
    больше нет специально — /api/save остался только для active_slot (какой
    орёл показан на сцене, чисто визуальный выбор без влияния на баланс).
    Старые поля не объявлены как required, поэтому кэшированный старый
    клиент, ещё шлющий их, не сломается — Pydantic просто их игнорирует."""
    user_id: int
    active_slot: int = 0
    ops: int = -1              # версия баланса, полученная при последней загрузке


class MissionClaim(BaseModel):
    user_id: int
    mission_id: str


class DailyClaim(BaseModel):
    user_id: int


class WheelSpin(BaseModel):
    user_id: int


class MerchantBuyMeat(BaseModel):
    user_id: int
    amount: float


class MerchantSellEagle(BaseModel):
    user_id: int
    slot_index: int


class FarmSlotAction(BaseModel):
    user_id: int
    slot_index: int


class FusionAttempt(BaseModel):
    user_id: int
    slot_a: int
    slot_b: int
    use_gram: bool = False


class BuySlot(BaseModel):
    user_id: int


class EggIndexAction(BaseModel):
    user_id: int
    index: int


class EggMergeAction(BaseModel):
    user_id: int
    from_index: int
    into_index: int


class UnlockEggSlot(BaseModel):
    user_id: int


class OpenAllEggs(BaseModel):
    user_id: int


class BuyVip(BaseModel):
    user_id: int
    tier_id: str


class NestAction(BaseModel):
    user_id: int


class NestUpgradeAction(BaseModel):
    user_id: int
    item_type: str
    grade: str


class NestEquipAction(BaseModel):
    user_id: int
    tier_id: str
    item_type: str
    grade: str


class NestUnequipAction(BaseModel):
    user_id: int
    tier_id: str
    item_type: str


class ArenaResult(BaseModel):
    user_id: int
    won: bool


class ArenaReveal(BaseModel):
    user_id: int
    match_token: str


class ArenaBuyEnergy(BaseModel):
    user_id: int
    currency: str  # "gold" | "gram"


class MarketListRequest(BaseModel):
    user_id: int
    monster_id: str
    price_gram: float


class MarketBuyRequest(BaseModel):
    user_id: int
    listing_id: str


class MarketCancelRequest(BaseModel):
    user_id: int
    listing_id: str


class EquipMarketListRequest(BaseModel):
    user_id: int
    item_type: str
    grade: str
    price_gram: float


class EquipMarketBuyRequest(BaseModel):
    user_id: int
    listing_id: str


class EquipMarketCancelRequest(BaseModel):
    user_id: int
    listing_id: str


class ResourceMarketListRequest(BaseModel):
    user_id: int
    resource: str  # "shards" | "particles"
    amount: int
    price_gram: float


class ResourceMarketBuyRequest(BaseModel):
    user_id: int
    listing_id: str


class ResourceMarketCancelRequest(BaseModel):
    user_id: int
    listing_id: str


class DepositCheck(BaseModel):
    user_id: int


class WalletSave(BaseModel):
    user_id: int
    address: str = ""


class WithdrawRequest(BaseModel):
    user_id: int
    address: str
    amount: float


class AdminLogin(BaseModel):
    password: str


class AdminPlayerUpdate(BaseModel):
    name: Optional[str] = None
    coins: Optional[float] = None
    mnstr: Optional[float] = None
    gold: Optional[float] = None
    slots: Optional[int] = None
    active_slot: Optional[int] = None
    wallet: Optional[str] = None
    monsters: Optional[List[dict]] = None
    eggs_board: Optional[List[int]] = None
    eggs_board_unlocked: Optional[int] = None
    vip_tier: Optional[str] = None
    vip_expires_at: Optional[float] = None


class AdminGrantShards(BaseModel):
    count: int = 1


class AdminGrantItem(BaseModel):
    item_type: str
    grade: str
    count: int = 1


class AdminConfigUpdate(BaseModel):
    config: dict


class AdminFeatureToggle(BaseModel):
    wheel_enabled: bool
    missions_enabled: bool
    maintenance_enabled: bool


# --- FASTAPI SETUP ---
app = FastAPI(title="SkyLords GRAMM")
app.mount("/assets", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="assets")

# Пока включены техработы, все /api/* эндпоинты (кроме самой проверки статуса)
# отвечают 503 — админка и статика (страница, конфиг, ассеты) продолжают
# работать как обычно, чтобы экран техработ на клиенте мог загрузиться.
MAINTENANCE_ALLOWED_PATHS = {"/api/maintenance"}


@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    if (
        MAINTENANCE_ENABLED
        and request.url.path.startswith("/api/")
        and request.url.path not in MAINTENANCE_ALLOWED_PATHS
    ):
        return JSONResponse(status_code=503, content={"detail": MAINTENANCE_MESSAGE})
    return await call_next(request)


@app.get("/api/maintenance")
async def maintenance_status():
    return {
        "enabled": MAINTENANCE_ENABLED,
        "message": MAINTENANCE_MESSAGE,
        "chat_url": MAINTENANCE_CHAT_URL,
    }


@app.get("/", response_class=HTMLResponse)
async def serve_webapp():
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/game_config.json")
async def serve_config():
    return FileResponse(CONFIG_PATH, media_type="application/json")


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    with open(os.path.join(BASE_DIR, "admin.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/load/{user_id}")
async def load_user_data(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Loads the farm - eagles stay put and just tick towards their next egg."""
    user_id = authenticate(x_telegram_init_data, user_id)

    context = signed_context(x_telegram_init_data)
    if context.get("start_param"):
        await attach_referrer(user_id, context["name"], context["start_param"])
    elif context.get("name"):
        await ensure_user(user_id, name=context["name"])

    await reconcile_arena_season()
    await reconcile_clan_tournament()
    row = await fetch_user(user_id)
    row = await accrue_vip_meat(user_id, row)
    row = await reconcile_queues(user_id, row)
    row = await reconcile_pvp_energy(user_id, row)
    row = await reconcile_nest_particles(user_id, row)
    farm = read_farm(row["monsters"])

    await store.update(user_id, {"monsters": farm, "last_seen": int(time.time())})

    return {
        "user_id": user_id,
        "coins": float(row.get("coins") or 0.0),
        "total_earned": float(row.get("total_earned") or 0.0),
        "mnstr": float(row.get("mnstr") or 0.0),
        "gold": float(row.get("gold") or 0.0),
        "monsters": farm,
        "farm_queue": read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX],
        "active_slot": int(row.get("active_slot") or 0),
        "missions": row.get("missions") or [],
        "slots": int(row.get("slots") or START_SLOTS),
        "referrals": int(row.get("referrals") or 0),
        "invited_by": await inviter_name(row.get("referred_by")),
        "daily": daily_state(row),
        "eggs_board": normalize_eggs_board(row.get("eggs_board")),
        "eggs_board_unlocked": max(2, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 2))),
        "eggs_queue": normalize_eggs_queue(row.get("eggs_queue")),
        "wallet": row.get("wallet") or "",
        "ops": int(row.get("ops") or 0),
        "vip_tier": row.get("vip_tier") or "",
        "vip_expires_at": float(row.get("vip_expires_at") or 0),
        "vip_last_meat_at": float(row.get("vip_last_meat_at") or 0),
        "merchant": await store.get_merchant_state(),
        "wheel": wheel_state(row),
        "nest": await nest_state_view(row),
        "pvp_rating": pvp_rating_of(row),
        "pvp_energy": row.get("pvp_energy", PVP_ENERGY_MAX),
        "pvp_energy_reset_at": arena_energy_reset_at(int(row.get("pvp_energy_day") or day_index())),
        "arena_season": arena_season_view(),
        "clan_id": row.get("clan_id"),
        "ton": ton_info(user_id),
        "operations": await store.recent_operations(user_id),
        "bot_username": BOT_USERNAME,
    }


@app.get("/api/referrals/{user_id}")
async def list_referrals(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Список приглашённых друзей и сумма их пополнений — для вкладки
    «Друзья». Бонус, начисленный с каждого, не хранится отдельной строкой
    (при пополнении просто прибавляется к балансу рефера, см.
    credit_deposits), поэтому пересчитывается здесь от той же ставки."""
    user_id = authenticate(x_telegram_init_data, user_id)
    friends = await store.list_referrals(user_id)
    for f in friends:
        f["bonus_earned"] = round(f["total_deposit"] * REFERRAL_SHARE, 9)
    return {"referral_share": REFERRAL_SHARE, "friends": friends}


@app.post("/api/save")
async def save_user_data(state: FarmState, x_telegram_init_data: Optional[str] = Header(None)):
    """Все балансы и состояние фермы теперь пишет сервер сам, атомарно, по
    месту действия (см. /api/farm/*, /api/eggs/*, /api/vip/*). Этот эндпоинт
    сохраняет только active_slot — какой орёл показан на сцене."""
    user_id = authenticate(x_telegram_init_data, state.user_id)
    row = await fetch_user(user_id)

    server_ops = int(row.get("ops") or 0)
    if state.ops >= 0 and state.ops != server_ops:
        return {"status": "stale", "ops": server_ops}

    await store.update(user_id, {"active_slot": max(0, state.active_slot), "last_seen": int(time.time())})
    return {"status": "success", "ops": server_ops}


# --- FARM/EGGS/VIP ACTIONS: атомарные, сервер сам считает и проверяет всё,
# клиент только шлёт намерение (какой слот/индекс) и показывает ответ. ---

@app.post("/api/farm/feed")
async def farm_feed(request: FarmSlotAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Тап кормления: списывает Meat, продвигает прогресс тапов; на
    feed_taps_per_level тапов запускает таймер яйца."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        farm = read_farm(row.get("monsters"))
        i = request.slot_index
        if not (0 <= i < len(farm)):
            raise HTTPException(status_code=404, detail="Слот не найден")
        slot = farm[i]
        if slot["expedition_until"] > 0:
            raise HTTPException(status_code=400, detail="Орёл в экспедиции")
        if slot["feed_level"] >= FEED_LEVELS:
            raise HTTPException(status_code=400, detail="Орёл уже прокачан до максимума")
        if slot["next_egg_at"] > 0:
            raise HTTPException(status_code=400, detail="Яйцо ещё не готово")
        cost = feed_cost(slot["id"])
        mnstr = float(row.get("mnstr") or 0)
        if mnstr < cost:
            raise HTTPException(status_code=400, detail="Не хватает Meat")

        slot["feed_taps"] += 1
        started_farming = False
        if slot["feed_taps"] >= FEED_TAPS_PER_LEVEL:
            slot["feed_taps"] = 0
            slot["next_egg_at"] = int(time.time() + egg_interval_seconds(row))
            started_farming = True

        mnstr -= cost
        fields = {"mnstr": mnstr, "monsters": farm}
        return fields, {"mnstr": mnstr, "slot": slot, "slot_index": i, "started_farming": started_farming}

    return await run_farm_action(user_id, compute)


@app.post("/api/farm/collect_egg")
async def farm_collect_egg(request: FarmSlotAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Собирает готовое яйцо со слота на доску (или в очередь, если доска
    занята) и поднимает орла на уровень."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        farm = read_farm(row.get("monsters"))
        i = request.slot_index
        if not (0 <= i < len(farm)):
            raise HTTPException(status_code=404, detail="Слот не найден")
        slot = farm[i]
        if slot["feed_level"] >= FEED_LEVELS or slot["next_egg_at"] <= 0 or time.time() < slot["next_egg_at"]:
            raise HTTPException(status_code=400, detail="Яйцо ещё не готово")

        level = tier_index(slot["id"]) + 1
        board = normalize_eggs_board(row.get("eggs_board"))
        queue = normalize_eggs_queue(row.get("eggs_queue"))
        unlocked = max(2, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 2)))
        place_egg_on_board_or_queue(board, queue, unlocked, level)

        slot["next_egg_at"] = 0
        slot["feed_level"] = min(FEED_LEVELS, slot["feed_level"] + 1)

        fields = {"monsters": farm, "eggs_board": board, "eggs_queue": queue}
        return fields, {"slot": slot, "slot_index": i, "eggs_board": board, "eggs_queue": queue}

    return await run_farm_action(user_id, compute)


@app.post("/api/farm/fusion_attempt")
async def farm_fusion_attempt(request: FusionAttempt, x_telegram_init_data: Optional[str] = Header(None)):
    """Платная попытка улучшения: сервер сам крутит рулетку
    (fusion.roulette_by_tier), чтобы исход нельзя было подделать. Пара орлов
    остаётся на месте при любом исходе, кроме success — тогда она сливается
    в одного орла следующей редкости (fuseEagles на клиенте раньше)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        farm = read_farm(row.get("monsters"))
        a_i, b_i = request.slot_a, request.slot_b
        if a_i == b_i or not (0 <= a_i < len(farm)) or not (0 <= b_i < len(farm)):
            raise HTTPException(status_code=404, detail="Слот не найден")
        a, b = farm[a_i], farm[b_i]
        if a["id"] != b["id"]:
            raise HTTPException(status_code=400, detail="Разные виды орлов")
        if a["feed_level"] < FEED_LEVELS or b["feed_level"] < FEED_LEVELS:
            raise HTTPException(status_code=400, detail="Орлы должны быть прокачаны до максимума")

        idx = tier_index(a["id"])
        next_tier = CONFIG["tiers"][idx + 1] if idx + 1 < len(CONFIG["tiers"]) else None
        if not next_tier:
            raise HTTPException(status_code=400, detail="Дальше улучшать некуда")
        segments = ROULETTE_BY_TIER[idx] if idx < len(ROULETTE_BY_TIER) else []
        if not segments:
            raise HTTPException(status_code=500, detail="Таблица улучшения не настроена")

        use_gram = request.use_gram
        cost = MERGE_COST_GRAM if use_gram else MERGE_COST_MEAT
        coins = float(row.get("coins") or 0)
        mnstr = float(row.get("mnstr") or 0)
        if use_gram:
            if coins < cost:
                raise HTTPException(status_code=400, detail="Не хватает GRAM")
            coins -= cost
        else:
            if mnstr < cost:
                raise HTTPException(status_code=400, detail="Не хватает Meat")
            mnstr -= cost

        seg_i = pick_weighted_index(segments)
        outcome = segments[seg_i]
        slots_count = int(row.get("slots") or START_SLOTS)
        farm_queue = read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX]

        fields = {"coins": coins, "mnstr": mnstr}
        response = {
            "outcome": outcome.get("type"), "segment_index": seg_i,
            "coins": coins, "mnstr": mnstr,
        }

        if outcome.get("type") == "success":
            next_monster_id = next_tier["monsters"][0]["id"]
            keep, drop = sorted((a_i, b_i))
            del farm[drop]
            del farm[keep]
            farm.append(new_slot(next_monster_id))
            active_slot = len(farm) - 1
            drain_farm_queue(farm, farm_queue, slots_count)
            fields.update({"monsters": farm, "farm_queue": farm_queue, "active_slot": active_slot})
            response.update({"monster": next_monster_id, "active_slot": active_slot})
        elif outcome.get("type") == "eagle":
            bonus_id = roll_monster(outcome.get("tier"))
            add_farm_slot(farm, farm_queue, slots_count, bonus_id)
            fields.update({"monsters": farm, "farm_queue": farm_queue})
            response["monster"] = bonus_id
        else:
            amount = float(outcome.get("amount") or 0)
            mnstr += amount
            fields["mnstr"] = mnstr
            response["mnstr"] = mnstr
            response["amount"] = amount

        response["monsters"] = fields.get("monsters", farm)
        response["farm_queue"] = fields.get("farm_queue", farm_queue)
        return fields, response

    return await run_farm_action(user_id, compute)


@app.post("/api/farm/expedition/start")
async def farm_expedition_start(request: FarmSlotAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Платно отправляет полностью прокачанного орла добывать золото."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        farm = read_farm(row.get("monsters"))
        i = request.slot_index
        if not (0 <= i < len(farm)):
            raise HTTPException(status_code=404, detail="Слот не найден")
        slot = farm[i]
        if slot["expedition_until"] > 0:
            raise HTTPException(status_code=400, detail="Орёл уже в экспедиции")
        if slot["feed_level"] < FEED_LEVELS:
            raise HTTPException(status_code=400, detail="В экспедицию берут только полностью прокачанных орлов")
        cost = EXPEDITION_COST_MEAT
        mnstr = float(row.get("mnstr") or 0)
        if mnstr < cost:
            raise HTTPException(status_code=400, detail="Не хватает Meat")

        slot["expedition_until"] = int(time.time() + expedition_duration_seconds(row))
        mnstr -= cost
        fields = {"monsters": farm, "mnstr": mnstr}
        return fields, {"slot": slot, "slot_index": i, "mnstr": mnstr}

    return await run_farm_action(user_id, compute)


@app.post("/api/farm/expedition/collect")
async def farm_expedition_collect(request: FarmSlotAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Забирает золото у вернувшегося из экспедиции орла."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        farm = read_farm(row.get("monsters"))
        i = request.slot_index
        if not (0 <= i < len(farm)):
            raise HTTPException(status_code=404, detail="Слот не найден")
        slot = farm[i]
        if slot["expedition_until"] <= 0 or time.time() < slot["expedition_until"]:
            raise HTTPException(status_code=400, detail="Экспедиция ещё не вернулась")

        reward = expedition_gold_reward(slot["id"])
        slot["expedition_until"] = 0
        gold = max(0.0, float(row.get("gold") or 0)) + reward
        fields = {"monsters": farm, "gold": gold}
        return fields, {"slot": slot, "slot_index": i, "gold": gold, "reward": reward}

    return await run_farm_action(user_id, compute)


@app.post("/api/farm/buy_slot")
async def farm_buy_slot(request: BuySlot, x_telegram_init_data: Optional[str] = Header(None)):
    """Покупает следующий слот фермы по фиксированной цене (slots.prices)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        slots_count = int(row.get("slots") or START_SLOTS)
        if slots_count >= MAX_SLOTS:
            raise HTTPException(status_code=400, detail="Все слоты уже открыты")
        cost = SLOTS_PRICES[slots_count] if slots_count < len(SLOTS_PRICES) else None
        if cost is None:
            raise HTTPException(status_code=500, detail="Цена слота не настроена")
        coins = float(row.get("coins") or 0)
        if coins < cost:
            raise HTTPException(status_code=400, detail="Не хватает GRAM")

        farm = read_farm(row.get("monsters"))
        farm_queue = read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX]
        new_slots_count = slots_count + 1
        drain_farm_queue(farm, farm_queue, new_slots_count)

        coins -= cost
        fields = {"coins": coins, "slots": new_slots_count, "monsters": farm, "farm_queue": farm_queue}
        return fields, {"coins": coins, "slots": new_slots_count, "monsters": farm, "farm_queue": farm_queue}

    return await run_farm_action(user_id, compute)


@app.post("/api/farm/delete_eagle")
async def farm_delete_eagle(request: FarmSlotAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Удаляет орла любого уровня насовсем взамен на фиксированную компенсацию
    Meat — последнего орла на ферме удалить нельзя, сцене нужен хотя бы один
    активный. Освободившийся слот сразу добирает орла из очереди."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        farm = read_farm(row.get("monsters"))
        i = request.slot_index
        if not (0 <= i < len(farm)):
            raise HTTPException(status_code=404, detail="Слот не найден")
        if len(farm) <= 1:
            raise HTTPException(status_code=400, detail="Нельзя остаться без орлов")

        monster_id = farm[i]["id"]
        del farm[i]
        farm_queue = read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX]
        slots_count = int(row.get("slots") or START_SLOTS)
        drain_farm_queue(farm, farm_queue, slots_count)
        active_slot = min(int(row.get("active_slot") or 0), len(farm) - 1)
        mnstr = float(row.get("mnstr") or 0) + EAGLE_DELETE_MEAT_REWARD

        fields = {"monsters": farm, "farm_queue": farm_queue, "active_slot": active_slot, "mnstr": mnstr}
        return fields, {
            "monster": monster_id, "mnstr": mnstr, "monsters": farm,
            "farm_queue": farm_queue, "active_slot": active_slot, "reward": EAGLE_DELETE_MEAT_REWARD,
        }

    return await run_farm_action(user_id, compute)


# --- ГНЕЗДО ВОИНОВ: Небесный Осколок (покупка, общий кулдаун и суточный
# лимит на всех игроков), пассивная добыча частичек, крафт и улучшение
# снаряжения, экипировка орлов. ---

@app.get("/api/nest/shard/state")
async def nest_shard_state():
    """Общий (не персональный) кулдаун и счётчик покупок Небесного Осколка —
    обновляется у всех игроков сразу после чьей-либо покупки. Клиент дёргает
    это при каждом открытии вкладки «Гнездо», как и /api/merchant/state для
    лавки купца, чтобы не ждать полной пересинхронизации аккаунта."""
    return await store.get_nest_state(day_index(), NEST_SHARD_DAILY_LIMIT)


@app.post("/api/nest/shard/buy")
async def nest_shard_buy(request: NestAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Небесный Осколок — общий (не персональный) ресурс: доступен строго 1
    за раз НА ВСЕХ игроков сразу, а не по одному на каждого — купил кто-то
    один, кулдаун встал для всех. Плюс суточный лимит в NEST_SHARD_DAILY_LIMIT
    покупок на всех игроков вместе (обнуляется по UTC-суткам, как daily/
    wheel). Купленный осколок достаётся только самому покупателю и навсегда
    остаётся в его Кузнице, пассивно добывая частички (см.
    nest_pending_particles) — он не расходуется."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)

    now = time.time()
    # Считаем новый last_claim ДО того, как число осколков реально
    # изменится — иначе новая (более высокая) ставка задним числом
    # применилась бы ко всему времени с прошлого сбора (см.
    # nest_settle_particles).
    new_last_claim = nest_settle_particles(row, now, nest_owned_shards(row) + 1)
    result = await store.buy_nest_shard(
        user_id, now, day_index(now), NEST_SHARD_PRICE_GRAM,
        NEST_SHARD_COOLDOWN_HOURS * 3600, NEST_SHARD_DAILY_LIMIT, new_last_claim,
    )
    if result["status"] == "cooldown":
        raise HTTPException(status_code=400, detail="Осколок ещё не готов")
    if result["status"] == "daily_limit":
        raise HTTPException(
            status_code=400,
            detail=f"Сегодня уже куплено {NEST_SHARD_DAILY_LIMIT} осколков — суточный лимит на всех игроков исчерпан",
        )
    if result["status"] == "insufficient_gram":
        raise HTTPException(status_code=400, detail="Не хватает GRAM")
    if result["status"] != "ok":
        raise HTTPException(status_code=409, detail="Осколок уже купили — попробуй ещё раз")

    row = await fetch_user(user_id)
    return {
        "status": "success",
        "coins": float(row.get("coins") or 0),
        "shard_cooldown_until": result["cooldown_until"],
        "shard_bought_today": result["bought_today"],
        "shard_daily_limit": NEST_SHARD_DAILY_LIMIT,
        "shard_count": nest_owned_shards(row),
        "last_claim": nest_last_claim_of(row, now),
    }


ARENA_MATCH_TTL_SECONDS = 300  # сколько живёт замаскированный подбор до раскрытия/протухания
# match_token -> {"user_id", "opponent_id", "expires_at"} — только подобранный
# соперник ждёт раскрытия перед боем; ничего секретного тут не хранится, но
# сам по себе словарь не должен расти бесконечно, поэтому чистим протухшие
# записи при каждом новом подборе (см. _prune_arena_matches).
ARENA_PENDING_MATCHES: dict = {}


def _prune_arena_matches() -> None:
    now = time.time()
    for token in [t for t, m in ARENA_PENDING_MATCHES.items() if m["expires_at"] < now]:
        ARENA_PENDING_MATCHES.pop(token, None)


def rating_bracket(rating: float) -> int:
    """Рейтинг соперника, округлённый до ближайшей сотни — это всё, что
    видно ДО боя (см. arena_opponent). Точная цифра позволила бы игроку
    найти соперника в Топ-100 по этому же значению рейтинга и заранее
    посмотреть его орла, поэтому точный pvp_rating на этом шаге на сервер
    вообще не уходит."""
    return round(rating / 100) * 100


@app.get("/api/arena/opponent")
async def arena_opponent(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Подбор соперника на Арене по месту в общей Таблице лидеров (PvP-рейтинг),
    а не по редкости орла — см. store.find_ladder_opponent: случайный игрок
    либо из ближайших ARENA_LADDER_ABOVE_COUNT мест НАД текущим игроком, либо
    в пределах ±ARENA_LADDER_RATING_RANGE очков рейтинга.

    КРИТИЧНО ДЛЯ БЕЗОПАСНОСТИ: на этом шаге отдаётся ТОЛЬКО округлённый до
    сотен рейтинг и одноразовый match_token — ни ник, ни точный рейтинг, ни
    редкость орла, ни снаряжение соперника клиенту не передаются. Иначе
    точная цифра рейтинга однозначно вычисляла бы конкретного игрока в
    Топ-100 ещё ДО начала боя. Точные данные раскрывает только
    /api/arena/reveal, который клиент дёргает в момент реального запуска
    анимации боя (см. arena_reveal)."""
    authenticate(x_telegram_init_data, user_id)
    row = await fetch_user(user_id)
    my_rating = pvp_rating_of(row)

    opponent = await store.find_ladder_opponent(
        user_id, my_rating, ARENA_LADDER_ABOVE_COUNT, ARENA_LADDER_RATING_RANGE,
    )
    if not opponent:
        return {"found": False}

    _prune_arena_matches()
    token = secrets.token_urlsafe(16)
    ARENA_PENDING_MATCHES[token] = {
        "user_id": user_id,
        "opponent_id": opponent["user_id"],
        "expires_at": time.time() + ARENA_MATCH_TTL_SECONDS,
    }
    return {
        "found": True,
        "match_token": token,
        "rating_bracket": rating_bracket(opponent["pvp_rating"]),
    }


@app.post("/api/arena/reveal")
async def arena_reveal(request: ArenaReveal, x_telegram_init_data: Optional[str] = Header(None)):
    """Раскрывает реального соперника, подобранного arena_opponent, по
    одноразовому match_token — вызывается клиентом строго в момент запуска
    анимации боя, не раньше. Токен привязан к user_id (нельзя раскрыть чужой
    подбор) и одноразовый (удаляется сразу при чтении), а протухшие токены
    в ARENA_PENDING_MATCHES не проходят проверку expires_at."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    match = ARENA_PENDING_MATCHES.pop(request.match_token, None)
    if not match or match["user_id"] != user_id or match["expires_at"] < time.time():
        return {"found": False}

    opponent_row = await store.get(match["opponent_id"])
    if not opponent_row:
        return {"found": False}

    tier_id = best_owned_tier(opponent_row.get("monsters"))
    equipped = normalize_nest_equipped(opponent_row.get("nest_equipped")).get(tier_id, {})
    return {
        "found": True,
        "name": opponent_row.get("name") or f"Игрок {match['opponent_id']}",
        "tier_id": tier_id,
        "equipped": equipped,
        "pvp_rating": pvp_rating_of(opponent_row),
    }


@app.post("/api/arena/result")
async def arena_result(request: ArenaResult, x_telegram_init_data: Optional[str] = Header(None)):
    """Итог боя на Арене (визуальный автобой) резолвится на клиенте — сюда
    приходит только won: сервер лишь сохраняет прирост/потерю PvP-рейтинга
    для Топ-100 (+PVP_RATING_WIN за победу, -PVP_RATING_LOSS за поражение,
    не ниже 0)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    current = pvp_rating_of(row)
    delta = PVP_RATING_WIN if request.won else -PVP_RATING_LOSS
    new_rating = max(0, current + delta)
    await store.update(user_id, {"pvp_rating": new_rating})
    return {"pvp_rating": new_rating}


@app.post("/api/arena/spend_energy")
async def arena_spend_energy(request: NestAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Списывает PVP_ENERGY_COST энергии за вход в бой на Арене — клиент
    вызывает это ровно один раз на каждый реальный запуск боя
    (arenaStartBattle), что для реального соперника, что для дикого орла.
    До списания пересчитывает суточное пополнение (см. pvp_energy_of), так
    что заход после долгого перерыва сперва honestly увидит полную шкалу."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        energy, day = pvp_energy_of(row)
        if energy < PVP_ENERGY_COST:
            raise HTTPException(status_code=400, detail="Нет энергии")
        energy -= PVP_ENERGY_COST
        fields = {"pvp_energy": energy, "pvp_energy_day": day}
        extra = {"pvp_energy": energy, "energy_reset_at": arena_energy_reset_at(day)}
        return fields, extra

    return await run_farm_action(user_id, compute)


@app.post("/api/arena/buy_energy")
async def arena_buy_energy(request: ArenaBuyEnergy, x_telegram_init_data: Optional[str] = Header(None)):
    """Докупка энергии Арены — 1⚡ за ARENA_ENERGY_PRICE_GOLD золота или за
    ARENA_ENERGY_PRICE_GRAM GRAM. Потолка PVP_ENERGY_MAX у покупки нет —
    донат может увести энергию выше дневного лимита, суточное пополнение
    его при этом никогда не опускает (см. pvp_energy_of)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    if request.currency not in ("gold", "gram"):
        raise HTTPException(status_code=400, detail="Неизвестная валюта")

    def compute(row):
        energy, day = pvp_energy_of(row)
        if request.currency == "gold":
            gold = float(row.get("gold") or 0)
            if gold < ARENA_ENERGY_PRICE_GOLD:
                raise HTTPException(status_code=400, detail="Не хватает золота")
            fields = {"gold": gold - ARENA_ENERGY_PRICE_GOLD, "pvp_energy": energy + 1, "pvp_energy_day": day}
            extra = {"pvp_energy": energy + 1, "gold": fields["gold"]}
        else:
            coins = float(row.get("coins") or 0)
            if coins < ARENA_ENERGY_PRICE_GRAM:
                raise HTTPException(status_code=400, detail="Не хватает GRAM")
            fields = {"coins": coins - ARENA_ENERGY_PRICE_GRAM, "pvp_energy": energy + 1, "pvp_energy_day": day}
            extra = {"pvp_energy": energy + 1, "coins": fields["coins"]}
        return fields, extra

    return await run_farm_action(user_id, compute)


@app.get("/api/arena/leaderboard")
async def arena_leaderboard(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Общая Таблица лидеров Арены — ВСЕ игроки по pvp_rating (никаких
    сгенерированных ботов), плюс место текущего игрока. Открытие таблицы —
    такая же точка проверки смены сезона, как и /api/load (см.
    reconcile_arena_season), чтобы сезон сменился не только у тех, кто
    успел перезайти в приложение."""
    authenticate(x_telegram_init_data, user_id)
    await reconcile_arena_season()
    row = await fetch_user(user_id)
    my_rating = pvp_rating_of(row)

    # Абсолютная анонимность: только место, ник и очки — редкость/грейд
    # орла (best_tier) намеренно не отдаём вообще, иначе по нему можно
    # было бы вычислить силу чужой птицы прямо из таблицы лидеров, даже не
    # заходя в бой (см. также двухфазный подбор соперника — arena_opponent/
    # arena_reveal — построенный на той же идее). limit=None — в таблицу
    # попадают ВСЕ игроки, не только верхушка.
    top_docs = await store.get_leaderboard(None)
    top = [
        {
            "user_id": doc["user_id"],
            "name": doc["name"] or f"Игрок {doc['user_id']}",
            "pvp_rating": int(doc["pvp_rating"]),
        }
        for doc in top_docs
    ]

    my_index = next((i for i, e in enumerate(top) if e["user_id"] == user_id), None)
    if my_index is not None:
        my_rank = my_index + 1
    else:
        my_rank = await store.count_higher_rating(my_rating) + 1

    return {
        "top": top,
        "my_rank": my_rank,
        "my_rating": my_rating,
        "season": arena_season_view(),
    }


# --- ПРИЗЫ ТУРНИРА АРЕНЫ (награда по итоговым местам Топ-50) ---
# Таблица наград (ARENA_SEASON_REWARDS) живёт в game_config.json ->
# arena_season.rewards, а не хардкодом здесь — клиент читает ровно ту же
# таблицу напрямую из CONFIG.arena_season.rewards и показывает её в
# Таблице лидеров, так что игроки видят точно то, что реально начислится
# (единый источник правды, см. renderLeaderboardRewards в index.html).

def arena_tournament_reward(rank: int) -> dict:
    """Приз турнира Арены по итоговому месту в Топ-50 (см.
    distribute_arena_rewards) — ищет место в ARENA_SEASON_REWARDS: либо
    точный "rank", либо диапазон "rank_from"..."rank_to". GRAMM
    начисляется строго на внутриигровой баланс (coins) — тот же баланс,
    которым игрок платит за всё внутри игры, — а НЕ отправляется на
    внешний TON-кошелёк; кошелёк/операции вывода здесь вообще не
    участвуют."""
    for tier in ARENA_SEASON_REWARDS:
        if "rank" in tier:
            if rank == tier["rank"]:
                return {"gram": tier.get("gram", 0), "shards": tier.get("shards", 0), "particles": tier.get("particles", 0)}
        elif tier.get("rank_from", 1) <= rank <= tier.get("rank_to", 0):
            return {"gram": tier.get("gram", 0), "shards": tier.get("shards", 0), "particles": tier.get("particles", 0)}
    return {"gram": 0, "shards": 0, "particles": 0}


async def distribute_arena_rewards() -> dict:
    """Начисляет призы турнира Арены по текущему Топ-50 (см.
    arena_tournament_reward) каждому награждённому — GRAMM на внутренний
    баланс (coins), Небесные Осколки в Кузницу (с пересчётом last_claim
    ДО добавления, как и в admin_grant_shards/nest_shard_buy — иначе
    пересчёт ставки задним числом обнулил бы или задвоил уже накопленный
    дробный прогресс, см. nest_settle_particles) и/или целые частички
    снаряжения (nest_particles) напрямую. Рейтинг игроков сама НЕ
    сбрасывает — это отдельный шаг у вызывающего (см. reconcile_arena_season
    для автоматического конца сезона и admin_distribute_arena_rewards для
    ручного запуска без сброса)."""
    top_docs = await store.get_leaderboard(50)
    now = time.time()
    awarded = []
    for rank, doc in enumerate(top_docs, start=1):
        reward = arena_tournament_reward(rank)
        if not (reward["gram"] or reward["shards"] or reward["particles"]):
            continue
        user_id = doc["user_id"]
        row = await store.get(user_id)
        if not row:
            continue

        fields = {}
        if reward["gram"]:
            fields["coins"] = float(row.get("coins") or 0) + reward["gram"]
        if reward["shards"]:
            miners = normalize_nest_miners(row.get("nest_miners"))
            new_last_claim = nest_settle_particles(row, now, len(miners) + reward["shards"])
            for i in range(reward["shards"]):
                miners.append({"id": f"reward-{user_id}-{int(now * 1000)}-{i}"})
            fields["nest_miners"] = miners
            fields["nest_last_claim"] = new_last_claim
        if reward["particles"]:
            fields["nest_particles"] = float(row.get("nest_particles") or 0) + reward["particles"]

        await store.update(user_id, fields)
        awarded.append({"user_id": user_id, "rank": rank, **reward})

    return {"rewarded": len(awarded), "details": awarded}


async def reconcile_arena_season() -> None:
    """Проверяет, не наступил ли новый сезон Арены (см. arena_season_index)
    — если да, ровно ОДИН из множества конкурентных вызовов выигрывает
    гонку за смену сезона (store.try_advance_arena_season — атомарный
    conditional update, как и общий кулдаун Небесного Осколка) и только он
    разносит призы по итоговому Топ-50 УХОДЯЩЕГО сезона (см.
    distribute_arena_rewards — считает по рейтингу ДО сброса, вызывается
    раньше reset_all_pvp_ratings строго в этом порядке), затем сбрасывает
    PvP-рейтинг всем игрокам к PVP_RATING_START для нового сезона. Вызывается
    на каждом /api/load и при каждом открытии Таблицы лидеров — пока сезон
    не сменился, это дешёвый no-op (один conditional update, всегда
    промахивающийся мимо фильтра)."""
    season = arena_season_index()
    if await store.try_advance_arena_season(season):
        await distribute_arena_rewards()
        await store.reset_all_pvp_ratings(PVP_RATING_START)


@app.post("/admin/api/arena/distribute_rewards")
async def admin_distribute_arena_rewards(_: None = Depends(require_admin)):
    """Ручной запуск начисления призов турнира Арены по текущему Топ-50 —
    жми из админки по факту окончания турнира (см. distribute_arena_rewards).
    Места/рейтинг игроков этим не сбрасываются."""
    return await distribute_arena_rewards()


# --- КЛАНЫ: создание/вступление, сжигание прокачанных орлов на силу
# клана, расстановка бойцов и турнирная сетка Топ-32 на вылет (24 дня,
# 5 раундов + матч за 3-е место — см. build_clan_bracket/reconcile_clan_tournament). ---

class ClanCreateRequest(BaseModel):
    user_id: int
    name: str


class ClanApplyRequest(BaseModel):
    user_id: int
    clan_id: str


class ClanAction(BaseModel):
    user_id: int


class ClanApplicantAction(BaseModel):
    user_id: int
    applicant_id: int


class ClanBurnRequest(BaseModel):
    user_id: int
    monster_id: str


class ClanLineupSubmitRequest(BaseModel):
    user_id: int
    tier_id: str


class ClanLineupApproveRequest(BaseModel):
    user_id: int
    member_ids: List[int]


def clan_view(clan: dict) -> dict:
    """Единая форма ответа о клане — и для «своего» клана, и (без лишних
    полей расстановки) для превью в Топе кланов."""
    return {
        "id": clan["id"],
        "name": clan.get("name") or "",
        "leader_id": clan.get("leader_id"),
        "members": list(clan.get("members") or []),
        "member_count": len(clan.get("members") or []),
        "member_limit": CLAN_MEMBER_LIMIT,
        "open_slots": int(clan.get("open_slots") or 0),
        "clan_power": float(clan.get("clan_power") or 0),
        "lineup_submissions": clan.get("lineup_submissions") or {},
        "approved_lineup": clan.get("approved_lineup") or [],
        "applications_count": len(clan.get("applications") or []),
    }


def strongest_eagle_info(row: dict) -> Optional[dict]:
    """Самый сильный орёл на ферме игрока — по редкости (тир), при равной
    редкости по уровню откорма. Нужен модерации заявок в клане, чтобы лидер
    видел, кого именно принимает, не открывая профиль кандидата отдельно."""
    farm = read_farm(row.get("monsters"))
    if not farm:
        return None
    best = max(farm, key=lambda m: (TIER_INDEX.get(MONSTER_TIER.get(m["id"]), 0), int(m.get("feed_level") or 0)))
    return {"tier_id": MONSTER_TIER.get(best["id"]), "feed_level": int(best.get("feed_level") or 1)}


def combat_eagle_stats(tier_id: str, equipped_for_tier: Optional[dict]) -> dict:
    """Итоговые боевые статы орла редкости tier_id — серверный порт
    nestComputeStatsFromEquipped (index.html): та же формула бонуса
    снаряжения (когти->atk, броня->def, маска->crit, амулет->hp+spd),
    только источник базовых статов — COMBAT_BASE_STATS, а не клиентский
    NEST_EAGLE_BASE_STATS. Нужен для авторитетной серверной симуляции
    боя кланов (см. clan_battle_simulate), которая, в отличие от 1v1
    Арены, должна разрешаться сама — без участия чьего-либо клиента."""
    base = COMBAT_BASE_STATS.get(tier_id) or {"hp": 100, "atk": 10, "def": 8, "crit": 5, "spd": 10}
    equipped = equipped_for_tier or {}
    bonus = {"hp": 0.0, "atk": 0.0, "def": 0.0, "crit": 0.0, "spd": 0.0}
    for item_type in NEST_TYPE_ORDER:
        grade = equipped.get(item_type)
        if not grade:
            continue
        pct = NEST_GRADE_BONUS.get(grade, 0.0)
        stat = (NEST_ITEM_TYPES.get(item_type) or {}).get("stat")
        if stat == "atk":
            bonus["atk"] += pct
        elif stat == "def":
            bonus["def"] += pct
        elif stat == "crit":
            bonus["crit"] += pct
        elif stat == "hpspd":
            bonus["hp"] += pct
            bonus["spd"] += pct
    return {
        "hp": round(base["hp"] * (1 + bonus["hp"] / 100)),
        "atk": round(base["atk"] * (1 + bonus["atk"] / 100)),
        "def": round(base["def"] * (1 + bonus["def"] / 100)),
        "crit": round(base["crit"] * (1 + bonus["crit"] / 100), 1),
        "spd": round(base["spd"] * (1 + bonus["spd"] / 100)),
    }


def _clan_roll_damage(rng: random.Random, attacker_stats: dict, defender_stats: dict) -> tuple:
    """Порт arenaRollDamage (index.html) — тот же урон/крит, но со своим
    random.Random(seed), чтобы бой кланов был воспроизводим (детерминирован
    номером цикла турнира и индексом матча, см. clan_battle_simulate)."""
    crit_chance = max(0.0, min(0.6, (attacker_stats.get("crit") or 0) / 100))
    crit = rng.random() < crit_chance
    raw = attacker_stats["atk"] * (100 / (100 + defender_stats["def"]))
    value = raw * (0.85 + rng.random() * 0.3)
    if crit:
        value *= 1.8
    return max(1, round(value)), crit


def clan_battle_simulate(seed: str, roster_a: list, roster_b: list) -> dict:
    """10х10 «Прямой Эфир»: king of the hill — сражаются только текущие
    передние бойцы сторон, при гибели одного из них следующий из его
    очереди выходит со свежим HP, а ПОБЕДИВШИЙ бой продолжает со своим
    ТЕКУЩИМ (не восстановленным) HP — лечения между дуэлями нет. Очерёдность
    первого удара в каждой новой дуэли решает сравнение скорости (как и в
    1v1 Арене), заново для каждой пары. roster_a/roster_b — списки бойцов
    {user_id, name, tier_id, stats}, до CLAN_ROSTER_SIZE каждый. Возвращает
    {"winner": "a"|"b"|None, "log": [...]} — log воспроизводится на клиенте
    Canvas-анимацией вкладки «Прямой Эфир»."""
    rng = random.Random(seed)
    queue_a = [dict(f, hp=f["stats"]["hp"]) for f in roster_a]
    queue_b = [dict(f, hp=f["stats"]["hp"]) for f in roster_b]
    if not queue_a or not queue_b:
        if queue_a and not queue_b:
            return {"winner": "a", "log": []}
        if queue_b and not queue_a:
            return {"winner": "b", "log": []}
        return {"winner": None, "log": []}

    log = []

    def enter(side, fighter):
        log.append({
            "type": "enter", "side": side, "user_id": fighter["user_id"], "name": fighter["name"],
            "tier_id": fighter["tier_id"], "hp": fighter["hp"], "max_hp": fighter["stats"]["hp"],
        })

    cur_a = queue_a.pop(0)
    cur_b = queue_b.pop(0)
    enter("a", cur_a)
    enter("b", cur_b)

    guard = 0
    while guard < 20000:
        guard += 1
        a_spd, b_spd = cur_a["stats"]["spd"], cur_b["stats"]["spd"]
        if a_spd > b_spd:
            attacker_side = "a"
        elif b_spd > a_spd:
            attacker_side = "b"
        else:
            attacker_side = "a" if rng.random() < 0.5 else "b"

        while cur_a["hp"] > 0 and cur_b["hp"] > 0:
            attacker = cur_a if attacker_side == "a" else cur_b
            defender = cur_b if attacker_side == "a" else cur_a
            dmg, crit = _clan_roll_damage(rng, attacker["stats"], defender["stats"])
            defender["hp"] = max(0, defender["hp"] - dmg)
            log.append({"type": "attack", "side": attacker_side, "value": dmg, "crit": crit,
                        "defender_hp": defender["hp"]})
            if defender["hp"] <= 0:
                break
            attacker_side = "b" if attacker_side == "a" else "a"

        if cur_a["hp"] <= 0:
            log.append({"type": "death", "side": "a", "user_id": cur_a["user_id"], "name": cur_a["name"]})
            if not queue_a:
                return {"winner": "b", "log": log}
            cur_a = queue_a.pop(0)
            enter("a", cur_a)
        elif cur_b["hp"] <= 0:
            log.append({"type": "death", "side": "b", "user_id": cur_b["user_id"], "name": cur_b["name"]})
            if not queue_b:
                return {"winner": "a", "log": log}
            cur_b = queue_b.pop(0)
            enter("b", cur_b)

    return {"winner": None, "log": log}  # защитный предел — на практике недостижим (урон >= 1/удар)


def _clan_seed_order(n: int) -> list:
    """Стандартная сетка посева плей-офф на вылет: для n=4 -> [1,4,2,3]
    (1v4, 2v3), для n=8 -> [1,8,4,5,2,7,3,6] — сильнейший всегда встречает
    самого слабого из оставшихся в своей половине сетки."""
    order = [1, 2]
    while len(order) < n:
        size = len(order)
        order = [x for s in order for x in (s, size * 2 + 1 - s)]
    return order


# Структура турнирной сетки Топ-32 на 24 дня (см. build_clan_bracket):
# раунд -> (сколько матчей, с какого дня начинается, сколько матчей в день).
CLAN_ROUND_SPECS = [
    ("r32", 16, 1, 2),   # 1/16 финала — дни 1-8, 2 матча/день
    ("r16", 8, 9, 1),    # 1/8 финала — дни 9-16, 1 матч/день
    ("r8", 4, 17, 1),    # 1/4 финала — дни 17-20
    ("r4", 2, 21, 1),    # 1/2 финала — дни 21-22
]
# Индекс первого матча раунда в плоском списке bracket и число матчей в нём.
CLAN_ROUND_OFFSETS = {"r32": (0, 16), "r16": (16, 8), "r8": (24, 4), "r4": (28, 2), "r3rd": (30, 1), "final": (31, 1)}


def build_clan_bracket(clans: list) -> list:
    """Строит турнирную сетку Топ-32 (см. reconcile_clan_tournament) —
    только первый раунд получает реальных участников (посев по clan_power,
    см. _clan_seed_order); все последующие матчи, включая матч за 3-е
    место и финал, начинаются с пустых слотов, которые заполняются по мере
    разрешения предыдущих матчей (см. _advance_clan_bracket). Если
    зарегистрированных кланов меньше 32 — недостающие места сетки первого
    раунда становятся техническими «бай» (пустой слот, автопобеда соперника)."""
    n = CLAN_TOURNAMENT_SIZE
    seeds = _clan_seed_order(n)
    slots = [None] * n
    for i, seed in enumerate(seeds):
        idx = seed - 1
        slots[i] = clans[idx] if idx < len(clans) else None

    bracket = []
    for round_idx, (round_key, match_count, day_start, per_day) in enumerate(CLAN_ROUND_SPECS):
        for i in range(match_count):
            a = slots[2 * i] if round_idx == 0 else None
            b = slots[2 * i + 1] if round_idx == 0 else None
            bracket.append({
                "round": round_key, "day": day_start + i // per_day,
                "clan_a_id": a["id"] if a else None, "clan_a_name": a["name"] if a else None,
                "clan_b_id": b["id"] if b else None, "clan_b_name": b["name"] if b else None,
                "resolved": False, "winner_id": None, "winner_name": None, "battle_log": [],
            })
    bracket.append({"round": "r3rd", "day": 23, "clan_a_id": None, "clan_a_name": None,
                     "clan_b_id": None, "clan_b_name": None, "resolved": False,
                     "winner_id": None, "winner_name": None, "battle_log": []})
    bracket.append({"round": "final", "day": 24, "clan_a_id": None, "clan_a_name": None,
                     "clan_b_id": None, "clan_b_name": None, "resolved": False,
                     "winner_id": None, "winner_name": None, "battle_log": []})
    return bracket


def _clan_next_match_for_winner(round_key: str, local_idx: int) -> Optional[tuple]:
    """Куда попадает победитель матча round_key[local_idx] — (индекс
    следующего матча в bracket, слот 'clan_a'/'clan_b'). None — для
    финала и матча за 3-е место (дальше сетки нет)."""
    next_offset = {"r32": 16, "r16": 24, "r8": 28}.get(round_key)
    if next_offset is not None:
        return (next_offset + local_idx // 2, "clan_a" if local_idx % 2 == 0 else "clan_b")
    if round_key == "r4":
        return (31, "clan_a" if local_idx == 0 else "clan_b")  # финал
    return None


def _clan_next_match_for_loser(round_key: str, local_idx: int) -> Optional[tuple]:
    """Проигравший полуфинала (round_key == 'r4') уходит в матч за 3-е
    место — единственный случай, когда исход матча кланов важен и
    победителю, и проигравшему."""
    if round_key == "r4":
        return (30, "clan_a" if local_idx == 0 else "clan_b")
    return None


async def _clan_roster_fighters(clan: Optional[dict]) -> list:
    """Собирает боевых бойцов клана из его approved_lineup — для каждой
    записи {user_id, tier_id} подтягивает текущее снаряжение владельца в
    Кузнице на этой редкости и считает итоговые статы (combat_eagle_stats).
    Игрок, покинувший клан или не имеющий орла этой редкости уже, просто
    выпадает из состава (его лучше было не звать на бой без свежего
    подтверждения — но сама расстановка это отдельно проверяет при подаче)."""
    if not clan:
        return []
    fighters = []
    for entry in (clan.get("approved_lineup") or [])[:CLAN_ROSTER_SIZE]:
        user_id = entry.get("user_id")
        tier_id = entry.get("tier_id")
        if tier_id not in TIER_INDEX or user_id is None:
            continue
        row = await store.get(user_id)
        if not row:
            continue
        equipped = normalize_nest_equipped(row.get("nest_equipped"))
        fighters.append({
            "user_id": user_id, "name": row.get("name") or "",
            "tier_id": tier_id, "stats": combat_eagle_stats(tier_id, equipped.get(tier_id)),
        })
    return fighters


def clan_tournament_cycle_index(moment: Optional[float] = None) -> int:
    """Порядковый номер турнирного цикла кланов — тот же принцип, что и
    arena_season_index, но с шагом CLAN_TOURNAMENT_DAYS суток."""
    return int((moment if moment is not None else time.time()) // (CLAN_TOURNAMENT_DAYS * 86400))


async def _advance_clan_bracket(tournament: dict) -> None:
    """Разрешает все матчи текущего турнира, чей день уже наступил и кто
    ещё не resolved — в порядке индекса bracket, так что победитель/
    проигравший матча первого раунда попадает в слот следующего раунда ещё
    в ЭТОМ ЖЕ проходе (см. _clan_next_match_for_winner/_loser), а не ждёт
    отдельного вызова. Каждое разрешение матча — свой независимый
    conditional update (store.resolve_clan_match), так что при гонке
    конкурентных вызовов (несколько игроков зашли в момент смены дня)
    исход запишет только один из них; остальные просто не находят что
    записать и идут дальше по сетке, уже видя чужую запись при следующем
    reconcile."""
    now = time.time()
    bracket = tournament["bracket"]
    start_at = tournament["start_at"]

    for idx, match in enumerate(bracket):
        if match.get("resolved"):
            continue
        if start_at + (match.get("day", 1) - 1) * 86400 > now:
            continue

        a_id, b_id = match.get("clan_a_id"), match.get("clan_b_id")
        winner_id = winner_name = None
        battle_log: list = []

        if a_id and b_id:
            fighters_a = await _clan_roster_fighters(await store.get_clan(a_id))
            fighters_b = await _clan_roster_fighters(await store.get_clan(b_id))
            if fighters_a and fighters_b:
                result = clan_battle_simulate(f"{tournament['cycle']}:{idx}", fighters_a, fighters_b)
                battle_log = result["log"]
                if result["winner"] == "a":
                    winner_id, winner_name = a_id, match.get("clan_a_name")
                elif result["winner"] == "b":
                    winner_id, winner_name = b_id, match.get("clan_b_name")
            elif fighters_a:
                winner_id, winner_name = a_id, match.get("clan_a_name")  # соперник не подал расстановку
            elif fighters_b:
                winner_id, winner_name = b_id, match.get("clan_b_name")
        elif a_id:
            winner_id, winner_name = a_id, match.get("clan_a_name")  # технический бай
        elif b_id:
            winner_id, winner_name = b_id, match.get("clan_b_name")

        if not await store.resolve_clan_match(idx, winner_id, winner_name, battle_log):
            continue  # уже разрешён другим конкурентным вызовом

        match["resolved"], match["winner_id"], match["winner_name"], match["battle_log"] = (
            True, winner_id, winner_name, battle_log,
        )

        round_key = match["round"]
        local_idx = idx - CLAN_ROUND_OFFSETS[round_key][0]
        if winner_id:
            target = _clan_next_match_for_winner(round_key, local_idx)
            if target:
                target_idx, slot = target
                if await store.set_clan_match_participant(target_idx, slot, winner_id, winner_name):
                    bracket[target_idx][f"{slot}_id"] = winner_id
                    bracket[target_idx][f"{slot}_name"] = winner_name
        if round_key == "r4":
            loser_id = b_id if winner_id == a_id else (a_id if winner_id == b_id else None)
            loser_name = match.get("clan_b_name") if loser_id == b_id else match.get("clan_a_name")
            target = _clan_next_match_for_loser(round_key, local_idx)
            if target and loser_id:
                target_idx, slot = target
                if await store.set_clan_match_participant(target_idx, slot, loser_id, loser_name):
                    bracket[target_idx][f"{slot}_id"] = loser_id
                    bracket[target_idx][f"{slot}_name"] = loser_name


async def reconcile_clan_tournament() -> None:
    """Лениво продвигает турнир кланов — та же гонка-и-победитель схема,
    что и у reconcile_arena_season: если текущий сохранённый цикл устарел,
    сначала строим новую сетку по текущему Топ-32 силы (build_clan_bracket)
    и пробуем её атомарно записать (store.try_start_clan_tournament) —
    выигрывает ровно один из конкурентных вызовов, остальные просто
    перечитывают то, что записал победитель. Затем — вне зависимости от
    того, кто именно запустил цикл — разрешаем все назревшие матчи
    (_advance_clan_bracket), что дешёвый no-op, пока день очередного матча
    не наступил."""
    cycle = clan_tournament_cycle_index()
    tournament = await store.get_clan_tournament()
    if tournament is None or int(tournament.get("cycle", -1)) < cycle:
        top_clans = await store.list_top_clans(CLAN_TOURNAMENT_SIZE)
        bracket = build_clan_bracket(top_clans)
        start_at = cycle * CLAN_TOURNAMENT_DAYS * 86400
        if await store.try_start_clan_tournament(cycle, start_at, bracket):
            tournament = {"_id": "current", "cycle": cycle, "start_at": start_at, "bracket": bracket}
        else:
            tournament = await store.get_clan_tournament()
    if tournament:
        await _advance_clan_bracket(tournament)


@app.get("/api/clan/mine")
async def clan_mine(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Клан текущего игрока (или null, если ни в одном не состоит) — для
    входного экрана раздела «Кланы» и вкладки «Мой Клан»."""
    user_id = authenticate(x_telegram_init_data, user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        return {"clan_id": None, "clan": None}
    clan = await store.get_clan(clan_id)
    if not clan:
        await store.update(user_id, {"clan_id": None})  # рассинхрон — клан уже удалён (see leave_clan)
        return {"clan_id": None, "clan": None}

    member_names = {}
    member_power = {}
    for member_id in clan.get("members") or []:
        member_row = await store.get(member_id)
        if member_row:
            member_names[str(member_id)] = member_row.get("name") or ""
            member_power[str(member_id)] = float(member_row.get("burned_power") or 0)

    return {
        "clan_id": clan_id, "clan": clan_view(clan), "is_leader": clan.get("leader_id") == user_id,
        "member_names": member_names, "member_power": member_power,
    }


@app.get("/api/clan/top")
async def clan_top(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Топ кланов по силе, отсортированный строго по убыванию clan_power —
    тот же порядок, что и посев турнирной сетки Топ-32 (см.
    build_clan_bracket). Каждому клану добавлен флаг applied — подавал ли
    ЭТОТ игрок заявку именно сюда (см. apply_to_clan), чтобы кнопка
    «Вступить» на клиенте сразу показывала «Заявка отправлена»."""
    user_id = authenticate(x_telegram_init_data, user_id)
    clans = await store.list_top_clans(100)
    out = []
    for c in clans:
        view = clan_view(c)
        view["applied"] = user_id in (c.get("applications") or [])
        out.append(view)
    return {"clans": out}


@app.post("/api/clan/create")
async def clan_create(request: ClanCreateRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Создаёт клан и сразу делает создателя лидером — стоит
    CLAN_CREATE_COST_GRAM GRAM + CLAN_CREATE_COST_GOLD золота +
    CLAN_CREATE_COST_MEAT мяса одновременно."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    name = (request.name or "").strip()[:24]
    if not name:
        raise HTTPException(status_code=400, detail="Введите название клана")

    status, clan_id = await store.create_clan(
        user_id, name, CLAN_CREATE_COST_GRAM, CLAN_CREATE_COST_GOLD, CLAN_CREATE_COST_MEAT,
        CLAN_INITIAL_OPEN_SLOTS,
    )
    if status != "ok":
        raise HTTPException(status_code=400, detail="Не хватает ресурсов или вы уже состоите в клане")

    clan = await store.get_clan(clan_id)
    return {"status": "success", "clan_id": clan_id, "clan": clan_view(clan)}


@app.post("/api/clan/apply")
async def clan_apply(request: ClanApplyRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Подаёт заявку на вступление в клан — не вступает сразу, ждёт решения
    клан-лидера (см. /api/clan/applications/accept и /reject). Разрешено
    только игроку без своего клана; сама заявка ничем не ограничена по
    заполненности клана — доступность мест проверяется лидером при приёме."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    if row.get("clan_id"):
        raise HTTPException(status_code=400, detail="Вы уже состоите в клане")

    result = await store.apply_to_clan(user_id, request.clan_id)
    if result != "ok":
        raise HTTPException(status_code=400, detail="Клан не найден")
    return {"status": "success"}


@app.get("/api/clan/applications")
async def clan_applications(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Список заявок на вступление в СВОЙ клан — только для лидера
    («Рассмотрение заявок» во вкладке «Мой Клан»). На каждой карточке —
    никнейм и самый сильный орёл кандидата (см. strongest_eagle_info),
    чтобы решение о приёме принималось не вслепую."""
    user_id = authenticate(x_telegram_init_data, user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")
    clan = await store.get_clan(clan_id)
    if not clan or clan.get("leader_id") != user_id:
        raise HTTPException(status_code=403, detail="Список заявок доступен только лидеру клана")

    applicants = []
    for applicant_id in clan.get("applications") or []:
        applicant_row = await store.get(applicant_id)
        if not applicant_row:
            continue
        applicants.append({
            "user_id": applicant_id, "name": applicant_row.get("name") or "",
            "strongest_eagle": strongest_eagle_info(applicant_row),
        })
    return {"applications": applicants}


@app.post("/api/clan/applications/accept")
async def clan_application_accept(request: ClanApplicantAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Лидер принимает заявку — кандидат становится участником, только если
    в клане ещё есть открытое место (см. accept_clan_application)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")

    result = await store.accept_clan_application(clan_id, user_id, request.applicant_id, CLAN_MEMBER_LIMIT)
    if result != "ok":
        messages = {
            "not_found": "Клан не найден",
            "not_leader": "Принимать заявки может только лидер клана",
            "not_applied": "Этот игрок уже не подавал заявку",
            "no_open_slot": "В клане нет свободных мест",
            "already_in_clan": "Игрок уже вступил в другой клан",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось принять заявку"))

    clan = await store.get_clan(clan_id)
    return {"status": "success", "clan": clan_view(clan)}


@app.post("/api/clan/applications/reject")
async def clan_application_reject(request: ClanApplicantAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Лидер отклоняет заявку — кандидат просто убирается из списка
    ожидающих, ничего другого не меняется."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")

    ok = await store.reject_clan_application(clan_id, user_id, request.applicant_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Не удалось отклонить заявку")
    return {"status": "success"}


@app.post("/api/clan/leave")
async def clan_leave(request: ClanAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Выход из клана — лидерство переходит следующему участнику, если
    выходит лидер (см. leave_clan)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")

    result = await store.leave_clan(user_id, clan_id)
    if result != "ok":
        raise HTTPException(status_code=400, detail="Не удалось покинуть клан")
    return {"status": "success"}


@app.post("/api/clan/open_slot")
async def clan_open_slot(request: ClanAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Лидер платит CLAN_SLOT_PRICE_GRAM GRAM за одно дополнительное место
    (до CLAN_MEMBER_LIMIT) — из изначальных CLAN_INITIAL_OPEN_SLOTS
    открытых при создании."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")

    result = await store.open_clan_slot(user_id, clan_id, CLAN_SLOT_PRICE_GRAM, CLAN_MEMBER_LIMIT)
    if result != "ok":
        messages = {
            "not_found": "Клан не найден",
            "not_leader": "Открывать места может только лидер клана",
            "slots_maxed": "Все места уже открыты",
            "insufficient_funds": "Не хватает GRAM",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось открыть место"))

    clan = await store.get_clan(clan_id)
    fresh = await store.get(user_id)
    return {"status": "success", "clan": clan_view(clan), "coins": float(fresh.get("coins") or 0.0)}


@app.post("/api/clan/burn")
async def clan_burn(request: ClanBurnRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Безвозвратно сжигает полностью прокачанного (FEED_LEVELS уровня)
    орла с фермы игрока — очки идут на ЕГО ЛИЧНЫЙ burned_power (прибавка
    зависит от редкости орла, см. CLAN_BURN_POWER_BY_TIER) и остаются с
    ним навсегда, даже если он потом покинет клан. Сила клана нигде явно
    не увеличивается — она считается на лету суммой burned_power текущих
    участников (см. get_clan)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")

    tier_id = MONSTER_TIER.get(request.monster_id)
    power = CLAN_BURN_POWER_BY_TIER.get(tier_id)
    if not power:
        raise HTTPException(status_code=400, detail="Этого орла нельзя пожертвовать клану")

    farm = await store.burn_eagle_for_clan_power(user_id, request.monster_id, FEED_LEVELS, power)
    if farm is None:
        raise HTTPException(status_code=400, detail="Нет такого прокачанного орла (7 ур.) на ферме")

    clan = await store.get_clan(clan_id)
    fresh = await store.get(user_id)
    return {
        "status": "success", "monsters": read_farm(farm), "clan": clan_view(clan),
        "burned_power": float(fresh.get("burned_power") or 0),
    }


@app.post("/api/clan/lineup/submit")
async def clan_lineup_submit(request: ClanLineupSubmitRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Участник подаёт вкладке «Расстановка» своего лучшего боевого орла
    (по редкости — снаряжение берётся из его Кузницы на этот тир на момент
    боя, см. _clan_roster_fighters, а не фиксируется здесь)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")
    if request.tier_id not in TIER_INDEX:
        raise HTTPException(status_code=400, detail="Неизвестная редкость")
    if not any(m.get("id") == request.tier_id for m in read_farm(row.get("monsters"))):
        raise HTTPException(status_code=400, detail="У вас нет орла этой редкости")

    ok = await store.submit_clan_lineup(clan_id, user_id, request.tier_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Не удалось сохранить расстановку")

    clan = await store.get_clan(clan_id)
    return {"status": "success", "clan": clan_view(clan)}


@app.post("/api/clan/lineup/approve")
async def clan_lineup_approve(request: ClanLineupApproveRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Лидер выбирает до CLAN_ROSTER_SIZE лучших поданных бойцов и
    утверждает состав — именно этот approved_lineup идёт в бой в
    «Битве Кланов» (см. _clan_roster_fighters)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")

    clan = await store.get_clan(clan_id)
    if not clan or clan.get("leader_id") != user_id:
        raise HTTPException(status_code=403, detail="Утвердить расстановку может только лидер клана")

    submissions = clan.get("lineup_submissions") or {}
    entries, seen = [], set()
    for uid in request.member_ids:
        if uid in seen:
            continue
        sub = submissions.get(str(uid))
        if not sub:
            continue
        entries.append({"user_id": uid, "tier_id": sub.get("tier_id")})
        seen.add(uid)
        if len(entries) >= CLAN_ROSTER_SIZE:
            break
    if not entries:
        raise HTTPException(status_code=400, detail="Нет ни одного бойца с поданной расстановкой")

    ok = await store.approve_clan_lineup(clan_id, user_id, entries)
    if not ok:
        raise HTTPException(status_code=400, detail="Не удалось утвердить расстановку")

    fresh = await store.get_clan(clan_id)
    return {"status": "success", "clan": clan_view(fresh)}


@app.get("/api/clan/tournament")
async def clan_tournament_view(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Турнирная сетка Топ-32 кланов целиком — вкладка «Битва Кланов».
    battle_log каждого матча здесь не отдаётся (может быть длинным) — за
    ним отдельно, см. /api/clan/tournament/match/{match_index}."""
    authenticate(x_telegram_init_data, user_id)
    await reconcile_clan_tournament()
    tournament = await store.get_clan_tournament()
    if not tournament:
        return {"cycle": 0, "start_at": 0, "bracket": []}
    bracket = [{k: v for k, v in match.items() if k != "battle_log"} for match in tournament["bracket"]]
    return {"cycle": tournament["cycle"], "start_at": tournament["start_at"], "bracket": bracket}


@app.get("/api/clan/tournament/match/{match_index}")
async def clan_tournament_match(match_index: int, user_id: int,
                                 x_telegram_init_data: Optional[str] = Header(None)):
    """Один разрешённый матч турнира вместе с battle_log — вкладка
    «Прямой Эфир» проигрывает его на Canvas дуэль за дуэлью."""
    authenticate(x_telegram_init_data, user_id)
    tournament = await store.get_clan_tournament()
    bracket = (tournament or {}).get("bracket") or []
    if not (0 <= match_index < len(bracket)):
        raise HTTPException(status_code=404, detail="Матч не найден")
    return bracket[match_index]


@app.post("/api/nest/particles/collect")
async def nest_particles_collect(request: NestAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Собирает накопленные частички — только целую часть (см.
    nest_pending_particles). Дробный остаток не сгорает: last_claim
    сдвигается вперёд ровно на время уже собранных целых частичек, так что
    остаток продолжает тикать с той же самой точки, а не обнуляется."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        now = time.time()
        row = dict(row)
        row["nest_last_claim"] = nest_last_claim_of(row, now)
        pending = nest_pending_particles(row, now)
        stable = int(pending)  # pending всегда >= 0, так что int() == floor()
        if stable < 1:
            raise HTTPException(status_code=400, detail="Пока нечего собирать")
        rate = nest_particle_rate_per_second(nest_owned_shards(row))
        new_last_claim = now - (pending - stable) / rate
        particles = float(row.get("nest_particles") or 0) + stable
        fields = {"nest_particles": particles, "nest_last_claim": new_last_claim}
        return fields, {
            "collected": stable, "particles": particles,
            "last_claim": new_last_claim, "shard_count": nest_owned_shards(row),
        }

    return await run_farm_action(user_id, compute)


@app.post("/api/nest/craft")
async def nest_craft(request: NestAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Крафт серого предмета — случайный тип снаряжения (когти/броня/маска/
    кольцо) с равным шансом, за NEST_CRAFT_COST_PARTICLES частичек снаряжения
    И NEST_CRAFT_COST_GOLD игрового Золота одновременно — оба ресурса
    обязательны, проверяются и списываются вместе."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        particles = float(row.get("nest_particles") or 0)
        if particles < NEST_CRAFT_COST_PARTICLES:
            raise HTTPException(status_code=400, detail=f"Нужно {NEST_CRAFT_COST_PARTICLES} частичек")
        gold = float(row.get("gold") or 0)
        if gold < NEST_CRAFT_COST_GOLD:
            raise HTTPException(status_code=400, detail=f"Недостаточно золота ({NEST_CRAFT_COST_GOLD:.0f} 🪙)")
        particles -= NEST_CRAFT_COST_PARTICLES
        gold -= NEST_CRAFT_COST_GOLD
        item_type = random.choice(NEST_TYPE_ORDER)
        grade = NEST_GRADES[0]
        inventory = normalize_nest_inventory(row.get("nest_inventory"))
        inventory[item_type][grade] += 1
        fields = {"nest_particles": particles, "gold": gold, "nest_inventory": inventory}
        return fields, {
            "particles": particles, "gold": gold,
            "inventory": inventory, "item_type": item_type, "grade": grade,
        }

    return await run_farm_action(user_id, compute)


@app.post("/api/nest/upgrade")
async def nest_upgrade(request: NestUpgradeAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Улучшение: NEST_UPGRADE_GROUP предметов одного грейда -> 1 предмет
    следующего, успех 100% (никакого шанса на неудачу, в отличие от слияния
    орлов)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    if request.item_type not in NEST_TYPE_ORDER:
        raise HTTPException(status_code=400, detail="Неизвестный тип снаряжения")
    next_grade = nest_next_grade(request.grade)
    if not next_grade:
        raise HTTPException(status_code=400, detail="Максимальный грейд уже достигнут")

    def compute(row):
        inventory = normalize_nest_inventory(row.get("nest_inventory"))
        if inventory[request.item_type][request.grade] < NEST_UPGRADE_GROUP:
            raise HTTPException(status_code=400, detail=f"Нужно {NEST_UPGRADE_GROUP} предмета этого грейда")
        inventory[request.item_type][request.grade] -= NEST_UPGRADE_GROUP
        inventory[request.item_type][next_grade] += 1
        fields = {"nest_inventory": inventory}
        return fields, {"inventory": inventory, "item_type": request.item_type, "grade": next_grade}

    return await run_farm_action(user_id, compute)


@app.post("/api/nest/equip")
async def nest_equip(request: NestEquipAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Экипирует предмет инвентаря на боевого орла указанной редкости —
    один орёл на редкость (как и вид орла на ферме), поэтому экипировка не
    привязана к конкретному слоту фермы. Ранее надетый в этот слот предмет
    возвращается в инвентарь."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    if request.item_type not in NEST_TYPE_ORDER:
        raise HTTPException(status_code=400, detail="Неизвестный тип снаряжения")
    if request.tier_id not in TIER_INDEX:
        raise HTTPException(status_code=400, detail="Неизвестная редкость орла")
    if request.grade not in NEST_GRADES:
        raise HTTPException(status_code=400, detail="Неизвестный грейд предмета")

    def compute(row):
        inventory = normalize_nest_inventory(row.get("nest_inventory"))
        if inventory[request.item_type][request.grade] <= 0:
            raise HTTPException(status_code=400, detail="Нет такого предмета в инвентаре")
        equipped = normalize_nest_equipped(row.get("nest_equipped"))
        prev = equipped[request.tier_id][request.item_type]
        inventory[request.item_type][request.grade] -= 1
        if prev:
            inventory[request.item_type][prev] += 1
        equipped[request.tier_id][request.item_type] = request.grade
        fields = {"nest_inventory": inventory, "nest_equipped": equipped}
        return fields, {"inventory": inventory, "equipped": equipped}

    return await run_farm_action(user_id, compute)


@app.post("/api/nest/unequip")
async def nest_unequip(request: NestUnequipAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Снимает предмет с боевого орла указанной редкости обратно в инвентарь."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    if request.item_type not in NEST_TYPE_ORDER:
        raise HTTPException(status_code=400, detail="Неизвестный тип снаряжения")
    if request.tier_id not in TIER_INDEX:
        raise HTTPException(status_code=400, detail="Неизвестная редкость орла")

    def compute(row):
        equipped = normalize_nest_equipped(row.get("nest_equipped"))
        prev = equipped[request.tier_id][request.item_type]
        if not prev:
            raise HTTPException(status_code=400, detail="Слот уже пуст")
        inventory = normalize_nest_inventory(row.get("nest_inventory"))
        inventory[request.item_type][prev] += 1
        equipped[request.tier_id][request.item_type] = None
        fields = {"nest_inventory": inventory, "nest_equipped": equipped}
        return fields, {"inventory": inventory, "equipped": equipped}

    return await run_farm_action(user_id, compute)


@app.post("/api/eggs/unlock_slot")
async def eggs_unlock_slot(request: UnlockEggSlot, x_telegram_init_data: Optional[str] = Header(None)):
    """Разблокирует следующую ячейку доски яиц по фиксированной цене
    (eggs.unlock_prices)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        unlocked = max(2, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 2)))
        if unlocked >= EGG_BOARD_SIZE:
            raise HTTPException(status_code=400, detail="Все ячейки уже открыты")
        cost = board_unlock_cost(unlocked)
        if cost is None:
            raise HTTPException(status_code=500, detail="Цена ячейки не настроена")
        coins = float(row.get("coins") or 0)
        if coins < cost:
            raise HTTPException(status_code=400, detail="Не хватает GRAM")

        board = normalize_eggs_board(row.get("eggs_board"))
        queue = normalize_eggs_queue(row.get("eggs_queue"))
        new_unlocked = unlocked + 1
        drain_egg_queue(board, queue, new_unlocked)

        coins -= cost
        fields = {"coins": coins, "eggs_board_unlocked": new_unlocked, "eggs_board": board, "eggs_queue": queue}
        return fields, {"coins": coins, "eggs_board_unlocked": new_unlocked, "eggs_board": board, "eggs_queue": queue}

    return await run_farm_action(user_id, compute)


@app.post("/api/eggs/open")
async def eggs_open(request: EggIndexAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Вскрывает одно яйцо по таблице его уровня: джекпот / обычный орёл / Meat."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        unlocked = max(2, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 2)))
        board = normalize_eggs_board(row.get("eggs_board"))
        i = request.index
        if not (0 <= i < unlocked) or not board[i]:
            raise HTTPException(status_code=400, detail="Яйцо не найдено")

        outcome = roll_egg_outcome(board[i])
        board[i] = 0
        queue = normalize_eggs_queue(row.get("eggs_queue"))
        drain_egg_queue(board, queue, unlocked)

        fields = {"eggs_board": board, "eggs_queue": queue}
        response = {"outcome": outcome["kind"], "eggs_board": board, "eggs_queue": queue}

        if outcome["kind"] in ("jackpot", "meat"):
            mnstr = float(row.get("mnstr") or 0) + outcome["amount"]
            fields["mnstr"] = mnstr
            response["mnstr"] = mnstr
            response["amount"] = outcome["amount"]
        else:
            farm = read_farm(row.get("monsters"))
            farm_queue = read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX]
            slots_count = int(row.get("slots") or START_SLOTS)
            bonus_id = roll_monster("common")
            add_farm_slot(farm, farm_queue, slots_count, bonus_id)
            fields.update({"monsters": farm, "farm_queue": farm_queue})
            response.update({"monster": bonus_id, "monsters": farm, "farm_queue": farm_queue})

        return fields, response

    return await run_farm_action(user_id, compute)


@app.post("/api/eggs/open_all")
async def eggs_open_all(request: OpenAllEggs, x_telegram_init_data: Optional[str] = Header(None)):
    """Вскрывает все яйца на доске одним действием, каждое по своему уровню."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        unlocked = max(2, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 2)))
        board = normalize_eggs_board(row.get("eggs_board"))
        queue = normalize_eggs_queue(row.get("eggs_queue"))
        mnstr = float(row.get("mnstr") or 0)
        farm = read_farm(row.get("monsters"))
        farm_queue = read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX]
        slots_count = int(row.get("slots") or START_SLOTS)

        meat_total = 0.0
        jackpot_total = 0.0
        eagle_ids = []
        opened = 0
        for i in range(unlocked):
            level = board[i]
            if not level:
                continue
            outcome = roll_egg_outcome(level)
            board[i] = 0
            opened += 1
            if outcome["kind"] == "jackpot":
                jackpot_total += outcome["amount"]
            elif outcome["kind"] == "eagle":
                bonus_id = roll_monster("common")
                eagle_ids.append(bonus_id)
                add_farm_slot(farm, farm_queue, slots_count, bonus_id)
            else:
                meat_total += outcome["amount"]

        if not opened:
            raise HTTPException(status_code=400, detail="Нечего вскрывать")

        drain_egg_queue(board, queue, unlocked)
        mnstr += meat_total + jackpot_total

        fields = {
            "eggs_board": board, "eggs_queue": queue, "mnstr": mnstr,
            "monsters": farm, "farm_queue": farm_queue,
        }
        response = {
            "opened": opened, "meat_total": meat_total, "jackpot_total": jackpot_total,
            "eagles": eagle_ids, "mnstr": mnstr, "eggs_board": board, "eggs_queue": queue,
            "monsters": farm, "farm_queue": farm_queue,
        }
        return fields, response

    return await run_farm_action(user_id, compute)


@app.post("/api/eggs/merge")
async def eggs_merge(request: EggMergeAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Слияние двух яиц одного уровня в одно яйцо уровнем выше, либо перенос
    яйца в пустую ячейку — любой другой запрос отклоняется, доску нельзя
    переписать в произвольное состояние с клиента."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        unlocked = max(2, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 2)))
        board = normalize_eggs_board(row.get("eggs_board"))
        f, t = request.from_index, request.into_index
        if f == t or not (0 <= f < unlocked) or not (0 <= t < unlocked) or not board[f]:
            raise HTTPException(status_code=400, detail="Недопустимое перемещение")

        if not board[t]:
            board[t] = board[f]
            board[f] = 0
            return {"eggs_board": board}, {"eggs_board": board}

        if board[t] == board[f] and board[t] < MAX_EGG_LEVEL:
            board[f] = 0
            board[t] += 1
            queue = normalize_eggs_queue(row.get("eggs_queue"))
            drain_egg_queue(board, queue, unlocked)
            return {"eggs_board": board, "eggs_queue": queue}, {"eggs_board": board, "eggs_queue": queue}

        raise HTTPException(status_code=400, detail="Нельзя слить эти яйца")

    return await run_farm_action(user_id, compute)


@app.post("/api/vip/buy")
async def vip_buy(request: BuyVip, x_telegram_init_data: Optional[str] = Header(None)):
    """Покупает VIP-тариф — только когда ни один тариф ещё не активен.
    Первый день Meat начисляется сразу, как и раньше на клиенте."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    tier = VIP_TIERS.get(request.tier_id)
    if not tier:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    def compute(row):
        if vip_active(row):
            raise HTTPException(status_code=400, detail="VIP уже активен")
        coins = float(row.get("coins") or 0)
        price = float(tier.get("price_gram") or 0)
        if coins < price:
            raise HTTPException(status_code=400, detail="Не хватает GRAM")

        now = time.time()
        expires_at = now + float(tier.get("duration_days") or 0) * 86400
        meat_per_day = float(tier.get("meat_per_day") or 0)
        mnstr = float(row.get("mnstr") or 0) + meat_per_day
        coins -= price

        fields = {
            "coins": coins, "mnstr": mnstr,
            "vip_tier": tier["id"], "vip_expires_at": expires_at, "vip_last_meat_at": now,
        }
        return fields, dict(fields)

    return await run_farm_action(user_id, compute)


async def channel_subscribed(user_id: int, chat: str) -> bool:
    """Спрашивает у Telegram, состоит ли игрок в канале."""
    if not BOT_TOKEN:
        print("[missions] channel_subscribed: BOT_TOKEN не задан")
        return False
    from telegram import Bot
    from telegram.error import TelegramError

    try:
        member = await Bot(BOT_TOKEN).get_chat_member(chat_id=chat, user_id=user_id)
    except TelegramError as e:
        # Частая причина: бот не добавлен в канал администратором — тогда
        # Telegram отказывает в getChatMember даже для реального подписчика.
        print(f"[missions] getChatMember({chat!r}, {user_id}) FAILED: {type(e).__name__}: {e}")
        return False
    return member.status in ("member", "administrator", "creator")


@app.post("/api/mission/claim")
async def claim_mission(request: MissionClaim, x_telegram_init_data: Optional[str] = Header(None)):
    """Проверяет условие задания и начисляет награду. Сервер — единственный источник наград."""
    if not MISSIONS_ENABLED:
        raise HTTPException(status_code=404, detail="Задания временно отключены")

    user_id = authenticate(x_telegram_init_data, request.user_id)
    mission = MISSIONS.get(request.mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Задание не найдено")

    row = await fetch_user(user_id)
    if request.mission_id in (row.get("missions") or []):
        raise HTTPException(status_code=409, detail="Награда уже получена")

    if mission["type"] == "channel":
        if not await channel_subscribed(user_id, mission["chat"]):
            raise HTTPException(status_code=400, detail="Подписка на канал не найдена")

    granted = await store.claim_mission(
        user_id, request.mission_id, mission["gram"], mission["mnstr"]
    )
    if not granted:
        raise HTTPException(status_code=409, detail="Награда уже получена")

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "mnstr": float(fresh.get("mnstr") or 0.0),
        "total_earned": float(fresh.get("total_earned") or 0.0),
        "missions": fresh.get("missions") or [],
        "ops": int(fresh.get("ops") or 0),
    }


@app.post("/api/daily/claim")
async def claim_daily(request: DailyClaim, x_telegram_init_data: Optional[str] = Header(None)):
    """Награда за ежедневный вход. День серии и награду считает сервер."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)

    today = day_index()
    if int(row.get("daily_last") or 0) == today:
        raise HTTPException(status_code=409, detail="Сегодня награда уже забрана")

    day = daily_state(row)["day"]
    first_lap = int(row.get("daily_cycles") or 0) == 0
    reward = daily_reward(day, first_lap)
    cycle_complete = day >= DAILY_DAYS

    # Орла некуда селить — открываем под него слот, чтобы награда не пропала.
    extra_slot = False
    if reward["monster"]:
        if reward["monster"] not in MONSTERS:
            raise HTTPException(status_code=500, detail="Орёл награды не найден")
        slots = int(row.get("slots") or START_SLOTS)
        if len(read_farm(row["monsters"])) >= slots:
            if slots >= MAX_SLOTS:
                raise HTTPException(status_code=400, detail="Все слоты заняты — освободи один")
            extra_slot = True

    granted = await store.claim_daily(
        user_id, today, day, reward["gram"], reward["mnstr"],
        reward["monster"], extra_slot, cycle_complete,
    )
    if not granted:
        raise HTTPException(status_code=409, detail="Сегодня награда уже забрана")

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "day": day,
        "reward": reward,
        "coins": float(fresh.get("coins") or 0.0),
        "mnstr": float(fresh.get("mnstr") or 0.0),
        "total_earned": float(fresh.get("total_earned") or 0.0),
        "monsters": read_farm(fresh["monsters"]),
        "slots": int(fresh.get("slots") or START_SLOTS),
        "daily": daily_state(fresh),
        "ops": int(fresh.get("ops") or 0),
    }


@app.post("/api/wheel/spin")
async def spin_wheel(request: WheelSpin, x_telegram_init_data: Optional[str] = Header(None)):
    """Крутит колесо фортуны за GRAM: первые wheel.cheap_spins прокрутов в
    сутки — по cheap_cost, дальше — по expensive_cost. Сектор выбирает
    сервер, чтобы клиент не мог подделать результат."""
    if not WHEEL_ENABLED or not WHEEL_SEGMENTS:
        raise HTTPException(status_code=404, detail="Колесо фортуны отключено")

    user_id = authenticate(x_telegram_init_data, request.user_id)
    await fetch_user(user_id)

    spend = await store.spend_wheel_spin(
        user_id, day_index(), WHEEL_CHEAP_SPINS, WHEEL_CHEAP_COST, WHEEL_EXPENSIVE_COST,
    )
    if spend["status"] == "insufficient_gram":
        raise HTTPException(status_code=400, detail=f"Не хватает GRAM: нужно {spend['cost']}")
    if spend["status"] != "ok":
        raise HTTPException(status_code=409, detail="Не удалось списать GRAM за прокрут, попробуй ещё раз")

    reward = wheel_pick()
    if reward["monster"] and reward["monster"] not in MONSTERS:
        raise HTTPException(status_code=500, detail="Орёл приза не найден")

    eggs_result = None
    if reward["egg_level"] and reward["egg_count"]:
        eggs_result = await grant_wheel_eggs(user_id, int(reward["egg_level"]), int(reward["egg_count"]))

    # Приз-орёл: сажаем в свободный открытый слот, а если ферма заполнена —
    # в очередь (см. apply_wheel_reward) — без бесплатного слота сверх купленных.
    fresh = await apply_wheel_reward(user_id, reward)
    response = {
        "status": "success",
        "segment": reward["index"],
        "spin_cost": spend["cost"],
        "reward": {
            "gram": reward["gram"], "mnstr": reward["mnstr"], "monster": reward["monster"],
            "egg_level": reward["egg_level"], "egg_count": reward["egg_count"],
        },
        "coins": float(fresh.get("coins") or 0.0),
        "mnstr": float(fresh.get("mnstr") or 0.0),
        "total_earned": float(fresh.get("total_earned") or 0.0),
        "monsters": read_farm(fresh["monsters"]),
        "farm_queue": read_farm(fresh.get("farm_queue"))[:FARM_QUEUE_MAX],
        "slots": int(fresh.get("slots") or START_SLOTS),
        "ops": int(fresh.get("ops") or 0),
        "wheel": wheel_state(fresh),
    }
    if eggs_result:
        response["reward"]["eggs_result"] = {"board": eggs_result["board"], "queue": eggs_result["queue"]}
        response["eggs_board"] = eggs_result["eggs_board"]
        response["eggs_queue"] = eggs_result["eggs_queue"]
    return response


@app.get("/api/merchant/state")
async def merchant_state():
    """Общий (один на всех игроков) остаток лимитов лавки купца — публичный,
    без привязки к конкретному пользователю."""
    return await store.get_merchant_state()


@app.post("/api/merchant/buy_meat")
async def merchant_buy_meat(request: MerchantBuyMeat, x_telegram_init_data: Optional[str] = Header(None)):
    """Покупка Meat за золото по фиксированному курсу — лимит общий на всех
    игроков (а не персональный), поэтому считает и проверяет его сервер."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    await ensure_user(user_id)

    cfg = CONFIG.get("merchant") or {}
    limit = float(cfg.get("meat_buy_limit", 1000))
    max_per_purchase = float(cfg.get("meat_buy_max_per_purchase", 5))
    rate = float(cfg.get("meat_per_gold", 5)) or 5.0

    requested = float(request.amount)
    if requested <= 0:
        raise HTTPException(status_code=400, detail="Укажи количество Meat")

    result = await store.buy_merchant_meat(user_id, requested, limit, max_per_purchase, rate)
    if result["status"] == "limit_reached":
        raise HTTPException(status_code=409, detail="Лимит покупки Meat исчерпан")
    if result["status"] == "insufficient_gold":
        raise HTTPException(status_code=400, detail="Недостаточно золота")

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "amount": result["amount"],
        "cost": result["cost"],
        "gold": float(fresh.get("gold") or 0.0),
        "mnstr": float(fresh.get("mnstr") or 0.0),
        "merchant": await store.get_merchant_state(),
        "ops": int(fresh.get("ops") or 0),
    }


@app.post("/api/merchant/sell_eagle")
async def merchant_sell_eagle(request: MerchantSellEagle, x_telegram_init_data: Optional[str] = Header(None)):
    """Продажа обычного (серого) орла за GRAM — лимит общий на всех игроков,
    поэтому и он, и сама ферма продавца проверяются/меняются на сервере."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    await ensure_user(user_id)

    cfg = CONFIG.get("merchant") or {}
    limit = int(cfg.get("eagle_sell_limit", 10))
    price = float(cfg.get("eagle_gram_price", 0.2))
    common_ids = {m_id for m_id, tier in MONSTER_TIER.items() if tier == "common"}

    result = await store.sell_merchant_eagle(user_id, request.slot_index, limit, price, common_ids, FEED_LEVELS)
    if result["status"] == "limit_reached":
        raise HTTPException(status_code=409, detail="Лимит продажи орлов исчерпан")
    if result["status"] == "not_found":
        raise HTTPException(status_code=400, detail="Орёл не найден")
    if result["status"] == "wrong_tier":
        raise HTTPException(status_code=400, detail="Купец берёт только обычных орлов")
    if result["status"] == "not_fed":
        raise HTTPException(status_code=400, detail="Купец берёт только полностью откормленных орлов")
    if result["status"] == "last_eagle":
        raise HTTPException(status_code=400, detail="Нельзя остаться без орлов")
    if result["status"] == "conflict":
        raise HTTPException(status_code=409, detail="Ферма изменилась — попробуй ещё раз")

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "total_earned": float(fresh.get("total_earned") or 0.0),
        "monsters": read_farm(fresh["monsters"]),
        "farm_queue": read_farm(fresh.get("farm_queue"))[:FARM_QUEUE_MAX],
        "active_slot": int(fresh.get("active_slot") or 0),
        "merchant": await store.get_merchant_state(),
        "ops": int(fresh.get("ops") or 0),
    }


def _market_tier_ok(monster_id: str) -> bool:
    """Только редкость «необычный» (зелёный) и выше — как и в прежнем NPC-магазине."""
    tier = MONSTER_TIER.get(monster_id)
    return tier is not None and TIER_INDEX.get(tier, -1) >= MARKET_MIN_TIER_INDEX


def _market_min_price(monster_id: str) -> float:
    """Минимальная цена лота для редкости орла (0, если для редкости не задана)."""
    tier = MONSTER_TIER.get(monster_id)
    return MARKET_MIN_PRICE.get(tier, 0.0)


@app.get("/api/market/listings")
async def market_listings(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Список активных лотов рынка — P2P-торговля орлами между игроками."""
    authenticate(x_telegram_init_data, user_id)
    return {"listings": await store.list_listings()}


@app.post("/api/market/list")
async def market_list(request: MarketListRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Выставляет прокачанного (макс. уровень) орла редкости необычный+ на продажу за GRAM."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    if request.monster_id not in MONSTERS or not _market_tier_ok(request.monster_id):
        raise HTTPException(status_code=400, detail="Этот орёл не продаётся на рынке")
    if not (request.price_gram > 0):
        raise HTTPException(status_code=400, detail="Цена должна быть больше нуля")
    min_price = _market_min_price(request.monster_id)
    if request.price_gram < min_price:
        raise HTTPException(
            status_code=400,
            detail=f"Минимальная цена для этой редкости — {min_price:g} GRAM",
        )

    row = await fetch_user(user_id)
    listing_id = await store.create_listing(
        user_id, row.get("name") or "", request.monster_id,
        FEED_LEVELS, request.price_gram, int(time.time()),
    )
    if listing_id is None:
        raise HTTPException(status_code=400, detail="Нет такого прокачанного орла на ферме")

    fresh = await store.get(user_id)
    return {"status": "success", "listing_id": listing_id, "monsters": read_farm(fresh["monsters"])}


@app.post("/api/market/buy")
async def market_buy(request: MarketBuyRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Покупает лот — сервер атомарно переводит GRAM и передаёт орла."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    result = await store.buy_listing(
        user_id, request.listing_id, FEED_LEVELS, MAX_SLOTS, MARKET_COMMISSION,
    )
    if result != "ok":
        messages = {
            "not_found": "Лот уже продан или снят с продажи",
            "own_listing": "Нельзя купить свой же лот",
            "insufficient_funds": "Не хватает GRAM",
            "no_room": "На ферме нет места — освободи слот",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось купить"))

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "monsters": read_farm(fresh["monsters"]),
        "slots": int(fresh.get("slots") or START_SLOTS),
    }


@app.post("/api/market/cancel")
async def market_cancel(request: MarketCancelRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Снимает свой лот с продажи — орёл возвращается на ферму."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    result = await store.cancel_listing(user_id, request.listing_id, FEED_LEVELS, MAX_SLOTS)
    if result != "ok":
        messages = {
            "not_found": "Лот уже продан или снят с продажи",
            "not_owner": "Это не твой лот",
            "no_room": "На ферме нет места — освободи слот",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось снять лот"))

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "monsters": read_farm(fresh["monsters"]),
        "slots": int(fresh.get("slots") or START_SLOTS),
    }


# --- РЫНОК СНАРЯЖЕНИЯ: P2P-торговля крафченным снаряжением по грейдам ---

@app.get("/api/market/equip/listings")
async def equip_market_listings(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Список активных лотов рынка снаряжения — P2P-торговля предметами
    Кузницы (когти/броня/маска/кольцо) между игроками, отдельно от Топ-100
    орлов и обычного рынка орлов."""
    authenticate(x_telegram_init_data, user_id)
    return {"listings": await store.list_equip_listings()}


@app.post("/api/market/equip/list")
async def equip_market_list(request: EquipMarketListRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Выставляет 1 шт. предмета инвентаря на продажу за GRAM — списывается
    сразу (см. store.create_equip_listing), как и орёл на обычном рынке."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    if request.item_type not in NEST_TYPE_ORDER:
        raise HTTPException(status_code=400, detail="Неизвестный тип снаряжения")
    if request.grade not in NEST_GRADES:
        raise HTTPException(status_code=400, detail="Неизвестный грейд предмета")
    if not (request.price_gram > 0):
        raise HTTPException(status_code=400, detail="Цена должна быть больше нуля")
    min_price = EQUIP_MARKET_MIN_PRICE.get(request.grade, 0.0)
    if request.price_gram < min_price:
        raise HTTPException(status_code=400, detail=f"Минимальная цена для этого грейда — {min_price:g} GRAM")

    row = await fetch_user(user_id)
    listing_id = await store.create_equip_listing(
        user_id, row.get("name") or "", request.item_type, request.grade, request.price_gram, int(time.time()),
    )
    if listing_id is None:
        raise HTTPException(status_code=400, detail="Нет такого предмета в инвентаре")

    fresh = await store.get(user_id)
    return {
        "status": "success", "listing_id": listing_id,
        "inventory": normalize_nest_inventory(fresh.get("nest_inventory")),
    }


@app.post("/api/market/equip/buy")
async def equip_market_buy(request: EquipMarketBuyRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Покупает лот снаряжения — сервер атомарно переводит GRAM и передаёт предмет."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    result = await store.buy_equip_listing(user_id, request.listing_id, EQUIP_MARKET_COMMISSION)
    if result != "ok":
        messages = {
            "not_found": "Лот уже продан или снят с продажи",
            "own_listing": "Нельзя купить свой же лот",
            "insufficient_funds": "Не хватает GRAM",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось купить"))

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "inventory": normalize_nest_inventory(fresh.get("nest_inventory")),
    }


@app.post("/api/market/equip/cancel")
async def equip_market_cancel(request: EquipMarketCancelRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Снимает свой лот снаряжения с продажи — предмет возвращается в инвентарь."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    result = await store.cancel_equip_listing(user_id, request.listing_id)
    if result != "ok":
        messages = {
            "not_found": "Лот уже продан или снят с продажи",
            "not_owner": "Это не твой лот",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось снять лот"))

    fresh = await store.get(user_id)
    return {"status": "success", "inventory": normalize_nest_inventory(fresh.get("nest_inventory"))}


# --- РЫНОК РЕСУРСОВ: P2P-торговля целыми Небесными Осколками и целыми
# частичками снаряжения. В отличие от орлов/снаряжения выше, оба ресурса —
# не дискретные предметы инвентаря, а количества (amount), и Осколки к тому
# же двигают ставку накопления частичек (см. nest_pending_particles),
# поэтому их списание/зачисление на СВОЁМ документе делает
# _adjust_user_resource ниже (игровая формула), а не сырой storage.py —
# сравни с equip-рынком выше, которому такая формула не нужна вовсе. ---

async def _adjust_user_resource(user_id: int, resource: str, delta: int) -> bool:
    """+delta зачисляет, -delta списывает |delta| штук ресурса на СВОЁМ ЖЕ
    документе игрока — только эта половина сделки (списание у продавца при
    выставлении лота; зачисление покупателю или возврат продавцу при
    отмене/откате). Лот и GRAM — забота вызывающих эндпоинтов ниже через
    storage.py. Возвращает False, если ушло бы в минус, или если все 5
    попыток съела гонка (как и в run_farm_action).

    Для 'shards' пересчитывает nest_last_claim ДО изменения числа осколков
    (nest_settle_particles) — та же защита от «задним числом» смены ставки,
    что и в nest_shard_buy/admin_grant_shards/distribute_arena_rewards."""
    for _ in range(5):
        row = await fetch_user(user_id)
        ops = int(row.get("ops") or 0)
        if resource == "shards":
            miners = normalize_nest_miners(row.get("nest_miners"))
            new_count = len(miners) + delta
            if new_count < 0:
                return False
            now = time.time()
            new_last_claim = nest_settle_particles(row, now, new_count)
            if delta > 0:
                miners.extend({"id": f"trade-{user_id}-{int(now * 1000)}-{i}"} for i in range(delta))
            else:
                miners = miners[:new_count]
            fields = {"nest_miners": miners, "nest_last_claim": new_last_claim}
        else:  # particles
            particles = float(row.get("nest_particles") or 0)
            if particles + delta < -1e-9:
                return False
            fields = {"nest_particles": max(0.0, particles + delta)}
        if await store.cas_update(user_id, fields, ops):
            return True
    return False


@app.get("/api/market/resources/listings")
async def resource_market_listings(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Список активных лотов рынка ресурсов (Небесные Осколки/частички)."""
    authenticate(x_telegram_init_data, user_id)
    return {"listings": await store.list_resource_listings()}


@app.post("/api/market/resources/list")
async def resource_market_list(request: ResourceMarketListRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Выставляет amount штук ресурса на продажу за GRAM — списывается
    сразу через _adjust_user_resource, лот создаётся только при успехе."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    if request.resource not in ("shards", "particles"):
        raise HTTPException(status_code=400, detail="Неизвестный ресурс")
    amount = int(request.amount)
    min_amount = RESOURCE_MARKET_MIN_AMOUNT.get(request.resource, 1)
    if amount < min_amount:
        raise HTTPException(status_code=400, detail=f"Минимум {min_amount} шт.")
    if not (request.price_gram > 0):
        raise HTTPException(status_code=400, detail="Цена должна быть больше нуля")
    min_price = RESOURCE_MARKET_MIN_PRICE.get(request.resource, 0.0)
    if request.price_gram < min_price:
        raise HTTPException(status_code=400, detail=f"Минимальная цена лота — {min_price:g} GRAM")

    row = await fetch_user(user_id)
    if not await _adjust_user_resource(user_id, request.resource, -amount):
        raise HTTPException(status_code=400, detail="Недостаточно ресурса для выставления лота")
    listing_id = await store.create_resource_listing(
        user_id, row.get("name") or "", request.resource, amount, request.price_gram, int(time.time()),
    )

    fresh = await store.get(user_id)
    return {
        "status": "success", "listing_id": listing_id,
        "particles": float(fresh.get("nest_particles") or 0),
        "shard_count": nest_owned_shards(fresh),
    }


@app.post("/api/market/resources/buy")
async def resource_market_buy(request: ResourceMarketBuyRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Покупает лот ресурса. Лот+GRAM обрабатывает storage.py, как и у
    остальных рынков; зачисление ресурса покупателю — отдельный шаг через
    _adjust_user_resource (нужна игровая формула для Осколков). Если этот
    шаг не удался (гонка на 5 попыток) — откатываем и GRAM покупателя, и
    сам лот, чтобы деньги не списались без товара; продавцу ничего не
    зачисляем раньше самого последнего шага именно поэтому."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    claim = await store.claim_resource_listing_for_buy(user_id, request.listing_id)
    if claim == "not_found":
        raise HTTPException(status_code=400, detail="Лот уже продан или снят с продажи")
    if claim == "own_listing":
        raise HTTPException(status_code=400, detail="Нельзя купить свой же лот")
    if claim == "insufficient_funds":
        raise HTTPException(status_code=400, detail="Не хватает GRAM")
    listing = claim  # GRAM покупателя уже списан на этом этапе

    granted = await _adjust_user_resource(user_id, listing["resource"], int(listing["amount"]))
    if not granted:
        await store.refund_failed_resource_purchase(user_id, listing)
        raise HTTPException(status_code=409, detail="Не удалось завершить покупку — попробуй ещё раз")

    await store.credit_resource_seller(listing["seller_id"], listing["price_gram"], RESOURCE_MARKET_COMMISSION)

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "particles": float(fresh.get("nest_particles") or 0),
        "shard_count": nest_owned_shards(fresh),
    }


@app.post("/api/market/resources/cancel")
async def resource_market_cancel(request: ResourceMarketCancelRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Снимает свой лот ресурса с продажи и возвращает штуки обратно."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    listing = await store.take_own_resource_listing(user_id, request.listing_id)
    if listing == "not_found":
        raise HTTPException(status_code=400, detail="Лот уже продан или снят с продажи")
    if listing == "not_owner":
        raise HTTPException(status_code=400, detail="Это не твой лот")

    if not await _adjust_user_resource(user_id, listing["resource"], int(listing["amount"])):
        await store.restore_resource_listing(listing)
        raise HTTPException(status_code=409, detail="Не удалось снять лот — попробуй ещё раз")

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "particles": float(fresh.get("nest_particles") or 0),
        "shard_count": nest_owned_shards(fresh),
    }


@app.get("/tonconnect-manifest.json")
async def tonconnect_manifest():
    """Манифест для TON Connect. Адрес берётся из WEB_APP_URL, чтобы не хардкодить домен."""
    base = (WEB_APP_URL or "").rstrip("/")
    return {
        "url": base,
        "name": "SkyLords GRAMM",
        "iconUrl": f"{base}/assets/coin.png",
    }


@app.post("/api/wallet")
async def save_wallet(request: WalletSave, x_telegram_init_data: Optional[str] = Header(None)):
    """Запоминает адрес кошелька игрока — на него уйдут выплаты."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    address = (request.address or "").strip()
    if address and not valid_address(address):
        raise HTTPException(status_code=400, detail="Некорректный адрес кошелька")

    await ensure_user(user_id)
    await store.update(user_id, {"wallet": address})
    return {"status": "success", "wallet": address}


@app.post("/api/ton/check")
async def check_deposits(request: DepositCheck, x_telegram_init_data: Optional[str] = Header(None)):
    """Проверяет пополнения по мемо прямо сейчас — не дожидаясь фоновой проверки."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    if not TON_WALLET:
        raise HTTPException(status_code=400, detail="Кошелёк проекта не настроен")

    await credit_deposits(force=True)
    fresh = await fetch_user(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "ops": int(fresh.get("ops") or 0),
        "operations": await store.recent_operations(user_id),
    }


@app.post("/api/ton/withdraw")
async def withdraw(request: WithdrawRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Списывает GRAM и ставит заявку на выплату. Отправку подтверждает владелец проекта."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    address = (request.address or "").strip()
    if not valid_address(address):
        raise HTTPException(status_code=400, detail="Некорректный адрес кошелька")

    amount = round(float(request.amount or 0), 9)
    if amount < MIN_WITHDRAW:
        raise HTTPException(status_code=400, detail=f"Минимум для вывода — {MIN_WITHDRAW:g} GRAM")

    row = await fetch_user(user_id)
    if float(row.get("coins") or 0.0) < amount:
        raise HTTPException(status_code=400, detail="Недостаточно GRAM на балансе")

    payout = round(amount * (1 - WITHDRAW_COMMISSION) / TON_RATE, 9)
    if not await store.request_withdraw(user_id, address, amount, payout, int(time.time())):
        raise HTTPException(status_code=400, detail="Недостаточно GRAM на балансе")

    await store.update(user_id, {"wallet": address})
    name = row.get("name") or user_id
    await tg_send(
        ADMIN_CHAT_ID,
        f"💸 Заявка на вывод\n\nИгрок: <b>{name}</b> (<code>{user_id}</code>)\n"
        f"Сумма: <b>{amount:g} GRAM</b>\n"
        f"К выплате (комиссия {WITHDRAW_COMMISSION * 100:g}%): <b>{payout:g} TON</b>\n"
        f"Адрес: <code>{address}</code>",
    )
    await tg_send(
        user_id,
        f"📨 Заявка на вывод <b>{amount:g} GRAM</b> принята.\n"
        f"На кошелёк придёт ≈ <b>{payout:g} TON</b> (комиссия {WITHDRAW_COMMISSION * 100:g}%).\n"
        f"Выплата придёт на <code>{address}</code> после проверки.",
    )

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "ops": int(fresh.get("ops") or 0),
        "operations": await store.recent_operations(user_id),
        "payout": payout,
    }


# --- АДМИН-ПАНЕЛЬ (/admin) ---
# Отдельная авторизация паролем (ADMIN_PASSWORD), не связанная с Telegram.

def player_summary(doc: dict) -> dict:
    farm = read_farm(doc.get("monsters"))
    last_seen = int(doc.get("last_seen") or 0)
    return {
        "user_id": doc.get("user_id"),
        "name": doc.get("name") or "",
        "coins": float(doc.get("coins") or 0.0),
        "mnstr": float(doc.get("mnstr") or 0.0),
        "gold": float(doc.get("gold") or 0.0),
        "total_earned": float(doc.get("total_earned") or 0.0),
        "slots": int(doc.get("slots") or START_SLOTS),
        "farm_count": len(farm),
        "referrals": int(doc.get("referrals") or 0),
        "wallet": doc.get("wallet") or "",
        "last_seen": last_seen,
        "online": (time.time() - last_seen) <= ONLINE_THRESHOLD_SECONDS,
        "vip_tier": doc.get("vip_tier") or "",
        "vip_expires_at": float(doc.get("vip_expires_at") or 0),
    }


@app.post("/admin/api/login")
async def admin_login(body: AdminLogin, response: Response):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD не задан на сервере")
    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Неверный пароль")

    token = secrets.token_urlsafe(32)
    ADMIN_SESSIONS[token] = time.time() + ADMIN_SESSION_TTL
    response.set_cookie(
        "admin_session", token, httponly=True, samesite="strict",
        max_age=ADMIN_SESSION_TTL, path="/admin",
    )
    return {"status": "success"}


@app.post("/admin/api/logout")
async def admin_logout(request: Request, response: Response):
    ADMIN_SESSIONS.pop(request.cookies.get("admin_session"), None)
    response.delete_cookie("admin_session", path="/admin")
    return {"status": "success"}


@app.get("/admin/api/me")
async def admin_me(_: None = Depends(require_admin)):
    return {"status": "success"}


@app.get("/admin/api/stats")
async def admin_stats(_: None = Depends(require_admin)):
    return await store.stats(online_since=time.time() - ONLINE_THRESHOLD_SECONDS)


@app.get("/admin/api/players")
async def admin_list_players(search: str = "", limit: int = 50, offset: int = 0,
                              _: None = Depends(require_admin)):
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    search = search.strip()
    items = await store.list_players(search, limit, offset)
    total = await store.count_players(search)
    return {"items": [player_summary(doc) for doc in items], "total": total}


@app.get("/admin/api/players/{user_id}")
async def admin_get_player(user_id: int, _: None = Depends(require_admin)):
    doc = await store.get(user_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    doc["monsters"] = read_farm(doc.get("monsters"))
    doc["farm_queue"] = read_farm(doc.get("farm_queue"))[:FARM_QUEUE_MAX]
    doc["eggs_board"] = normalize_eggs_board(doc.get("eggs_board"))
    doc["eggs_queue"] = normalize_eggs_queue(doc.get("eggs_queue"))
    doc["nest_miners"] = normalize_nest_miners(doc.get("nest_miners"))
    doc["nest_inventory"] = normalize_nest_inventory(doc.get("nest_inventory"))
    doc["nest_equipped"] = normalize_nest_equipped(doc.get("nest_equipped"))
    return doc


@app.post("/admin/api/players/{user_id}")
async def admin_update_player(user_id: int, body: AdminPlayerUpdate,
                               _: None = Depends(require_admin)):
    doc = await store.get(user_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Игрок не найден")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="Нечего сохранять")

    if "coins" in fields:
        fields["coins"] = max(0.0, float(fields["coins"]))
    if "mnstr" in fields:
        fields["mnstr"] = max(0.0, float(fields["mnstr"]))
    if "gold" in fields:
        fields["gold"] = max(0.0, float(fields["gold"]))
    if "slots" in fields:
        fields["slots"] = max(START_SLOTS, min(int(fields["slots"]), MAX_SLOTS))
    if "active_slot" in fields:
        fields["active_slot"] = max(0, int(fields["active_slot"]))
    if "monsters" in fields:
        fields["monsters"] = read_farm(fields["monsters"])
    if "eggs_board" in fields:
        max_level = len(CONFIG["tiers"])
        board = normalize_eggs_board(fields["eggs_board"])
        fields["eggs_board"] = [min(max_level, v) for v in board]
    if "eggs_board_unlocked" in fields:
        fields["eggs_board_unlocked"] = max(2, min(EGG_BOARD_SIZE, int(fields["eggs_board_unlocked"])))

    await store.update(user_id, fields)
    fresh = await store.get(user_id)
    fresh["monsters"] = read_farm(fresh.get("monsters"))
    return fresh


@app.post("/admin/api/players/{user_id}/grant_shards")
async def admin_grant_shards(user_id: int, body: AdminGrantShards, _: None = Depends(require_admin)):
    """Начисляет игроку N Небесных Осколков напрямую, в обход общего (на
    всех игроков) кулдауна и суточного лимита покупки — это админский
    подарок, а не покупка. Осколки сразу учитываются в общей ставке
    накопления частичек (см. nest_pending_particles), как и купленные за
    GRAM; last_claim пересчитывается ДО добавления (nest_settle_particles),
    чтобы уже накопленный дробный прогресс не потерялся."""
    doc = await store.get(user_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Игрок не найден")

    count = max(1, min(int(body.count), 100))
    now = time.time()
    miners = normalize_nest_miners(doc.get("nest_miners"))
    new_last_claim = nest_settle_particles(doc, now, len(miners) + count)
    for i in range(count):
        miners.append({"id": f"admin-{user_id}-{int(now * 1000)}-{i}"})

    await store.update(user_id, {"nest_miners": miners, "nest_last_claim": new_last_claim})
    return {"nest_miners": miners, "shard_count": len(miners), "last_claim": new_last_claim}


@app.post("/admin/api/players/{user_id}/grant_item")
async def admin_grant_item(user_id: int, body: AdminGrantItem, _: None = Depends(require_admin)):
    """Начисляет игроку N предметов снаряжения Гнезда Воинов (когти/броня/
    маска/амулет, любой грейд) напрямую в инвентарь — админский подарок в
    обход крафта/улучшения."""
    doc = await store.get(user_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    if body.item_type not in NEST_TYPE_ORDER:
        raise HTTPException(status_code=400, detail="Неизвестный тип снаряжения")
    if body.grade not in NEST_GRADES:
        raise HTTPException(status_code=400, detail="Неизвестный грейд предмета")

    count = max(1, min(int(body.count), 999))
    inventory = normalize_nest_inventory(doc.get("nest_inventory"))
    inventory[body.item_type][body.grade] += count

    await store.update(user_id, {"nest_inventory": inventory})
    return {"nest_inventory": inventory}


@app.get("/admin/api/withdrawals")
async def admin_list_withdrawals(status: str = "", limit: int = 50, offset: int = 0,
                                  _: None = Depends(require_admin)):
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    items = await store.list_withdrawals(status.strip() or None, limit, offset)
    return {"items": items}


@app.post("/admin/api/withdrawals/{wd_id}/approve")
async def admin_approve_withdrawal(wd_id: str, _: None = Depends(require_admin)):
    wd_id = int(wd_id) if wd_id.isdigit() else wd_id
    if not await store.set_withdrawal_status(wd_id, "approved", refund=False):
        raise HTTPException(status_code=409, detail="Заявка уже обработана")
    return {"status": "success"}


@app.post("/admin/api/withdrawals/{wd_id}/reject")
async def admin_reject_withdrawal(wd_id: str, _: None = Depends(require_admin)):
    wd_id = int(wd_id) if wd_id.isdigit() else wd_id
    if not await store.set_withdrawal_status(wd_id, "rejected", refund=True):
        raise HTTPException(status_code=409, detail="Заявка уже обработана")
    return {"status": "success"}


@app.get("/admin/api/config")
async def admin_get_config(_: None = Depends(require_admin)):
    return CONFIG


@app.post("/admin/api/config")
async def admin_update_config(body: AdminConfigUpdate, _: None = Depends(require_admin)):
    cfg = body.config
    try:
        assert isinstance(cfg["tiers"], list) and cfg["tiers"]
        assert isinstance(cfg["missions"], list)
        assert isinstance(cfg["slots"], dict)
    except (AssertionError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="В конфиге не хватает обязательных разделов")

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    apply_config(cfg)
    return {"status": "success"}


@app.post("/admin/api/merchant/reset")
async def admin_reset_merchant(_: None = Depends(require_admin)):
    return await store.reset_merchant_state()


@app.post("/admin/api/features")
async def admin_set_features(body: AdminFeatureToggle, _: None = Depends(require_admin)):
    """Включает/выключает колесо фортуны, задания и режим техработ без правки сырого конфига."""
    cfg = dict(CONFIG)
    cfg["wheel"] = dict(cfg.get("wheel") or {})
    cfg["wheel"]["enabled"] = body.wheel_enabled
    cfg["missions_enabled"] = body.missions_enabled
    cfg["maintenance"] = dict(cfg.get("maintenance") or {})
    cfg["maintenance"]["enabled"] = body.maintenance_enabled

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    apply_config(cfg)
    return {
        "wheel_enabled": WHEEL_ENABLED,
        "missions_enabled": MISSIONS_ENABLED,
        "maintenance_enabled": MAINTENANCE_ENABLED,
    }


# --- TELEGRAM BOT LOGIC ---
def display_name(user) -> str:
    """Имя для показа: @username, иначе имя и фамилия."""
    if getattr(user, "username", None):
        return f"@{user.username}"
    parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
    return " ".join(p for p in parts if p).strip() or "Игрок"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    name = display_name(user)
    starter = CONFIG["tiers"][0]["monsters"][0]["name"]
    total_monsters = sum(len(t["monsters"]) for t in CONFIG["tiers"])

    referrer = None
    if context.args:
        try:
            candidate = int(context.args[0])
            if candidate != user_id:
                referrer = candidate
        except ValueError:
            referrer = None

    is_new = await ensure_user(user_id, referrer, name)
    # Имя могло смениться с прошлого запуска.
    await store.update(user_id, {"name": name})

    if is_new and referrer:
        await ensure_user(referrer)
        await store.increment(referrer, {"referrals": 1})
        await notify_referrer(referrer, name)

    row = await store.get(user_id)
    invited_by = await inviter_name((row or {}).get("referred_by"))

    if not is_new:
        text = (
            f"🦅 С возвращением, {name}!\n\n"
            "Твои орлы несли яйца, пока тебя не было — загляни на ферму."
        )
    elif invited_by:
        text = (
            f"🤝 <b>{invited_by}</b> позвал тебя в <b>SkyLords GRAMM</b>!\n\n"
            "Теперь друг будет получать процент GRAM с твоих пополнений.\n\n"
            f"🥚 Тебе уже выдан первый орёл — <b>{starter}</b>. Раз в сутки он "
            "приносит яйцо — сливай их на поле и получай Meat или новых орлов.\n"
            f"💎 Открывай слоты, покупай новых и собери всех {total_monsters} существ.\n"
            f"👥 Зови своих друзей — получай {round(REFERRAL_SHARE * 100)}% GRAM с их пополнений.\n\n"
            "Ферма ждёт 👇"
        )
    else:
        text = (
            "🦅 Добро пожаловать в <b>SkyLords GRAMM</b>!\n\n"
            f"🥚 Тебе уже выдан первый орёл — <b>{starter}</b>. Раз в сутки он "
            "приносит яйцо — сливай их на поле и получай Meat или новых орлов.\n"
            f"💎 Открывай слоты, покупай новых и собери всех {total_monsters} существ.\n"
            f"👥 Зови друзей — получай {round(REFERRAL_SHARE * 100)}% GRAM с их пополнений.\n\n"
            "Ферма ждёт 👇"
        )

    keyboard = [[InlineKeyboardButton("💎 Открыть SkyLords GRAMM", web_app=WebAppInfo(url=WEB_APP_URL))]]
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def run_bot():
    global BOT_USERNAME

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))

    await application.initialize()
    # Имя из getMe надёжнее ручной переменной: без опечаток и лишней @.
    me = await application.bot.get_me()
    if me.username:
        BOT_USERNAME = me.username
        print(f"Bot username: @{BOT_USERNAME}")
    await application.start()
    await application.updater.start_polling()

    while True:
        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup_event():
    try:
        await store.init()
        print("[storage] init() OK — подключение к базе рабочее, индексы/коллекции созданы")
    except Exception as e:
        print(f"[storage] init() FAILED: {type(e).__name__}: {e}")
        raise
    if BOT_TOKEN and WEB_APP_URL:
        asyncio.create_task(run_bot())
    else:
        print("BOT_TOKEN / WEB_APP_URL are not set - running the web app without the bot.")

    if TON_WALLET:
        asyncio.create_task(ton_poller())
        print(f"TON: пополнения проверяются каждые {TON_POLL_SECONDS} с на {TON_WALLET}")
    else:
        print("TON_WALLET is not set - the wallet section is disabled.")


if __name__ == "__main__":
    # Платформы (Railway, Render, Heroku, Fly) сами выдают порт в $PORT
    # и маршрутизируют только на него.
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
