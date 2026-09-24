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
    global REFERRAL_SHARE, MAX_EGG_LEVEL
    global MAINTENANCE, MAINTENANCE_ENABLED, MAINTENANCE_MESSAGE, MAINTENANCE_CHAT_URL
    global EGGS_CFG, EGG_INTERVAL_HOURS, UNLOCK_PRICES
    global HATCH_COMMON_BY_LEVEL, HATCH_COMMON_DEFAULT, HATCH_MEAT_MIN, HATCH_MEAT_MAX
    global HATCH_JACKPOT_CHANCE, HATCH_JACKPOT_MEAT_BY_LEVEL
    global FEED_BASE_COST, FEED_GROWTH, FEED_TAPS_PER_LEVEL, MERGE_COST_MEAT, MERGE_COST_GRAM, ROULETTE_BY_TIER
    global EXPEDITIONS_CFG, EXPEDITION_DURATION_HOURS, EXPEDITION_COST_MEAT, EXPEDITION_GOLD_BY_TIER
    global SLOTS_PRICES, VIP_TIERS
    global NEST_CFG, NEST_SHARD_PRICE_GRAM, NEST_SHARD_COOLDOWN_HOURS, NEST_PARTICLE_INTERVAL_HOURS
    global NEST_CRAFT_COST_PARTICLES, NEST_UPGRADE_GROUP, NEST_GRADES, NEST_GRADE_BONUS
    global NEST_ITEM_TYPES, NEST_TYPE_ORDER, NEST_SHARD_DAILY_LIMIT

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
    NEST_UPGRADE_GROUP = int(NEST_CFG.get("upgrade_group", 4))
    NEST_GRADES = list(NEST_CFG.get("grades") or ["grey", "green", "blue", "purple", "gold", "mythic"])
    NEST_GRADE_BONUS = {k: float(v) for k, v in (NEST_CFG.get("grade_bonus_pct") or {}).items()}
    NEST_ITEM_TYPES = NEST_CFG.get("item_types") or {}
    NEST_TYPE_ORDER = list(NEST_ITEM_TYPES.keys()) or ["claws", "armor", "mask", "ring"]


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
    miners = []
    if isinstance(raw, list):
        for m in raw:
            if isinstance(m, dict) and m.get("id"):
                try:
                    miners.append({"id": str(m["id"]), "last_collect_at": float(m.get("last_collect_at") or 0)})
                except (TypeError, ValueError):
                    pass
    return miners


def nest_miner_pending(miner: dict, now: float) -> int:
    interval = NEST_PARTICLE_INTERVAL_HOURS * 3600
    if interval <= 0:
        return 0
    return int((now - float(miner.get("last_collect_at") or 0)) // interval)


def nest_total_pending(miners: List[dict], now: float) -> int:
    return sum(nest_miner_pending(m, now) for m in miners)


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
    return {
        "shard_price_gram": NEST_SHARD_PRICE_GRAM,
        "shard_cooldown_until": shard["cooldown_until"],
        "shard_bought_today": shard["bought_today"],
        "shard_daily_limit": shard["daily_limit"],
        "particles": float(row.get("nest_particles") or 0),
        "miners": normalize_nest_miners(row.get("nest_miners")),
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
            "nest_inventory": {},
            "nest_equipped": {},
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

    row = await fetch_user(user_id)
    row = await accrue_vip_meat(user_id, row)
    row = await reconcile_queues(user_id, row)
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
    nest_miner_pending) — он не расходуется."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    await fetch_user(user_id)

    now = time.time()
    result = await store.buy_nest_shard(
        user_id, now, day_index(now), NEST_SHARD_PRICE_GRAM,
        NEST_SHARD_COOLDOWN_HOURS * 3600, NEST_SHARD_DAILY_LIMIT,
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
        "nest_miners": normalize_nest_miners(row.get("nest_miners")),
    }


@app.post("/api/nest/particles/collect")
async def nest_particles_collect(request: NestAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Собирает накопленные частички со всех осколков — офлайн-safe: время,
    не кратное 24ч на осколок, не сгорает (last_collect_at сдвигается только
    на целое число уже собранных интервалов, как и в reconcile_queues)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        now = time.time()
        miners = normalize_nest_miners(row.get("nest_miners"))
        collected = 0
        for m in miners:
            pending = nest_miner_pending(m, now)
            if pending > 0:
                collected += pending
                m["last_collect_at"] += pending * NEST_PARTICLE_INTERVAL_HOURS * 3600
        if collected <= 0:
            raise HTTPException(status_code=400, detail="Пока нечего собирать")
        particles = float(row.get("nest_particles") or 0) + collected
        fields = {"nest_miners": miners, "nest_particles": particles}
        return fields, {"collected": collected, "particles": particles, "nest_miners": miners}

    return await run_farm_action(user_id, compute)


@app.post("/api/nest/craft")
async def nest_craft(request: NestAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Крафт серого предмета за частички — случайный тип снаряжения
    (когти/броня/маска/кольцо) с равным шансом."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        particles = float(row.get("nest_particles") or 0)
        if particles < NEST_CRAFT_COST_PARTICLES:
            raise HTTPException(status_code=400, detail=f"Нужно {NEST_CRAFT_COST_PARTICLES} частичек")
        particles -= NEST_CRAFT_COST_PARTICLES
        item_type = random.choice(NEST_TYPE_ORDER)
        grade = NEST_GRADES[0]
        inventory = normalize_nest_inventory(row.get("nest_inventory"))
        inventory[item_type][grade] += 1
        fields = {"nest_particles": particles, "nest_inventory": inventory}
        return fields, {"particles": particles, "inventory": inventory, "item_type": item_type, "grade": grade}

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
    подарок, а не покупка. Каждый осколок — отдельный "майнер" в
    nest_miners, который сразу начинает добывать частички (last_collect_at
    = сейчас), точно как купленный за GRAM."""
    doc = await store.get(user_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Игрок не найден")

    count = max(1, min(int(body.count), 100))
    now = time.time()
    miners = normalize_nest_miners(doc.get("nest_miners"))
    for i in range(count):
        miners.append({"id": f"admin-{user_id}-{int(now * 1000)}-{i}", "last_collect_at": now})

    await store.update(user_id, {"nest_miners": miners})
    return {"nest_miners": miners}


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
