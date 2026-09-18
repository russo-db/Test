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
from fastapi.responses import HTMLResponse, FileResponse
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
FARM_QUEUE_MAX = 300  # защитный предел на длину очереди орлов, не помещающихся в открытые слоты

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
    global CONFIG, MISSIONS, QUALIFY_MNSTR, MONSTERS, STARTER_MONSTER
    global START_SLOTS, MAX_SLOTS
    global FUSION_CFG, FEED_LEVELS, DAILY, DAILY_DAYS, DAILY_STEP, DAILY_SPECIAL
    global WHEEL, WHEEL_SEGMENTS, WHEEL_ENABLED, WHEEL_CHEAP_SPINS, WHEEL_CHEAP_COST, WHEEL_EXPENSIVE_COST
    global MISSIONS_ENABLED
    global TON, TON_RATE, MIN_DEPOSIT, MIN_WITHDRAW, MEMO_PREFIX, WITHDRAW_COMMISSION
    global MONSTER_TIER, TIER_INDEX, MARKET_CFG, MARKET_MIN_TIER_INDEX, MARKET_COMMISSION, MARKET_MIN_PRICE
    global REFERRAL_SHARE, MAX_EGG_LEVEL

    CONFIG = cfg
    MISSIONS = {m["id"]: m for m in CONFIG["missions"]}
    QUALIFY_MNSTR = CONFIG["referral"].get("qualify_mnstr", 10)
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
    bonus = CONFIG["referral"].get("bonus", 0)
    fields = {"referrals": 1}
    if bonus:
        fields.update({"coins": bonus, "total_earned": bonus})
    await store.increment(referrer, fields)
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
    await tg_send(
        referrer,
        f"🎉 К тебе присоединился <b>{friend_name}</b>!\n\n"
        f"Друг засчитается в награды, когда намайнит {QUALIFY_MNSTR} Meat.",
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
            "mnstr": 0.0,
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
            "eggs_board_unlocked": 1,
            "eggs_queue": [],
            "wallet": "",
            "ops": 0,
            "vip_tier": "",
            "vip_expires_at": 0,
            "vip_last_meat_at": 0,
            "wheel_day": 0,
            "wheel_spins_today": 0,
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
    user_id: int
    coins: float
    total_earned: float
    mnstr: float = 0.0
    gold: float = 0.0
    monsters: List[dict]       # one slot per eagle: {"id", "next_egg_at", "feed_level", "feed_taps", "expedition_until"}
    farm_queue: List[dict] = []
    active_slot: int = 0
    missions: list = []
    slots: int = START_SLOTS
    eggs_board: List[int] = []
    eggs_board_unlocked: int = 1
    eggs_queue: List[int] = []
    ops: int = -1              # версия баланса, полученная при последней загрузке
    vip_tier: str = ""
    vip_expires_at: float = 0
    vip_last_meat_at: float = 0


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


class AdminConfigUpdate(BaseModel):
    config: dict


class AdminFeatureToggle(BaseModel):
    wheel_enabled: bool
    missions_enabled: bool


# --- FASTAPI SETUP ---
app = FastAPI(title="SkyLords GRAMM")
app.mount("/assets", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="assets")


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
        "referrals_qualified": await store.count_referrals(user_id, QUALIFY_MNSTR),
        "invited_by": await inviter_name(row.get("referred_by")),
        "daily": daily_state(row),
        "eggs_board": normalize_eggs_board(row.get("eggs_board")),
        "eggs_board_unlocked": max(1, min(EGG_BOARD_SIZE, int(row.get("eggs_board_unlocked") or 1))),
        "eggs_queue": normalize_eggs_queue(row.get("eggs_queue")),
        "wallet": row.get("wallet") or "",
        "ops": int(row.get("ops") or 0),
        "vip_tier": row.get("vip_tier") or "",
        "vip_expires_at": float(row.get("vip_expires_at") or 0),
        "vip_last_meat_at": float(row.get("vip_last_meat_at") or 0),
        "merchant": await store.get_merchant_state(),
        "wheel": wheel_state(row),
        "ton": ton_info(user_id),
        "operations": await store.recent_operations(user_id),
        "bot_username": BOT_USERNAME,
    }


@app.post("/api/save")
async def save_user_data(state: FarmState, x_telegram_init_data: Optional[str] = Header(None)):
    """Сохраняет ферму. Награды за задания сюда не приходят — их выдаёт сервер."""
    user_id = authenticate(x_telegram_init_data, state.user_id)
    row = await fetch_user(user_id)

    # Сервер мог начислить награду или пополнение уже после того, как клиент
    # прочитал баланс. Тогда его копия устарела — сохранять её нельзя, иначе
    # начисление затрётся. Клиент увидит "stale" и перезагрузит состояние.
    server_ops = int(row.get("ops") or 0)
    if state.ops >= 0 and state.ops != server_ops:
        return {"status": "stale", "ops": server_ops}

    eggs_board = normalize_eggs_board(state.eggs_board)
    eggs_board_unlocked = max(1, min(EGG_BOARD_SIZE, int(state.eggs_board_unlocked or 1)))
    eggs_queue = normalize_eggs_queue(state.eggs_queue)

    await store.update(
        user_id,
        {
            "coins": state.coins,
            "total_earned": state.total_earned,
            "mnstr": state.mnstr,
            "gold": max(0.0, state.gold),
            "monsters": read_farm(state.monsters),
            "farm_queue": read_farm(state.farm_queue)[:FARM_QUEUE_MAX],
            "active_slot": state.active_slot,
            "slots": state.slots,
            "eggs_board": eggs_board,
            "eggs_board_unlocked": eggs_board_unlocked,
            "eggs_queue": eggs_queue,
            "vip_tier": state.vip_tier,
            "vip_expires_at": max(0.0, state.vip_expires_at),
            "vip_last_meat_at": max(0.0, state.vip_last_meat_at),
            "last_seen": int(time.time()),
        },
    )
    return {"status": "success", "ops": server_ops}


async def channel_subscribed(user_id: int, chat: str) -> bool:
    """Спрашивает у Telegram, состоит ли игрок в канале."""
    if not BOT_TOKEN:
        return False
    from telegram import Bot
    from telegram.error import TelegramError

    try:
        member = await Bot(BOT_TOKEN).get_chat_member(chat_id=chat, user_id=user_id)
    except TelegramError:
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

    if mission["type"] == "referrals":
        have = await store.count_referrals(user_id, QUALIFY_MNSTR)
        if have < mission["need"]:
            raise HTTPException(
                status_code=400,
                detail=f"Засчитано друзей: {have} из {mission['need']}",
            )
    elif mission["type"] == "channel":
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
    row = await fetch_user(user_id)

    spend = await store.spend_wheel_spin(
        user_id, day_index(), WHEEL_CHEAP_SPINS, WHEEL_CHEAP_COST, WHEEL_EXPENSIVE_COST,
    )
    if spend["status"] == "insufficient_gram":
        raise HTTPException(status_code=400, detail=f"Не хватает GRAM: нужно {spend['cost']}")
    if spend["status"] != "ok":
        raise HTTPException(status_code=409, detail="Не удалось списать GRAM за прокрут, попробуй ещё раз")

    reward = wheel_pick()

    # Приз-орёл: сажаем в свободный слот, а если ферма заполнена — открываем
    # ещё один (как и в ежедневном входе), чтобы приз не пропал зря.
    extra_slot = False
    if reward["monster"]:
        if reward["monster"] not in MONSTERS:
            raise HTTPException(status_code=500, detail="Орёл приза не найден")
        slots = int(row.get("slots") or START_SLOTS)
        if len(read_farm(row["monsters"])) >= slots:
            if slots >= MAX_SLOTS:
                raise HTTPException(status_code=400, detail="Все слоты заняты — освободи один")
            extra_slot = True

    await store.claim_wheel(
        user_id, reward["gram"], reward["mnstr"], reward["monster"], extra_slot,
    )

    fresh = await store.get(user_id)
    return {
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
        "slots": int(fresh.get("slots") or START_SLOTS),
        "ops": int(fresh.get("ops") or 0),
        "wheel": wheel_state(fresh),
    }


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
        "last_seen": int(doc.get("last_seen") or 0),
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
    return await store.stats()


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
        fields["eggs_board_unlocked"] = max(1, min(EGG_BOARD_SIZE, int(fields["eggs_board_unlocked"])))

    await store.update(user_id, fields)
    fresh = await store.get(user_id)
    fresh["monsters"] = read_farm(fresh.get("monsters"))
    return fresh


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
    """Включает/выключает колесо фортуны и задания без правки сырого конфига."""
    cfg = dict(CONFIG)
    cfg["wheel"] = dict(cfg.get("wheel") or {})
    cfg["wheel"]["enabled"] = body.wheel_enabled
    cfg["missions_enabled"] = body.missions_enabled

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    apply_config(cfg)
    return {"wheel_enabled": WHEEL_ENABLED, "missions_enabled": MISSIONS_ENABLED}


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
        bonus = CONFIG["referral"].get("bonus", 0)
        fields = {"referrals": 1}
        if bonus:
            fields.update({"coins": bonus, "total_earned": bonus})
        await store.increment(referrer, fields)
        await notify_referrer(referrer, name)

    row = await store.get(user_id)
    invited_by = await inviter_name((row or {}).get("referred_by"))

    if not is_new:
        text = (
            f"🐲 С возвращением, {name}!\n\n"
            "Твои орлы несли яйца, пока тебя не было — загляни на ферму."
        )
    elif invited_by:
        text = (
            f"🤝 <b>{invited_by}</b> позвал тебя в <b>SkyLords GRAMM</b>!\n\n"
            f"Теперь ты в его команде: как только намайнишь {QUALIFY_MNSTR} Meat, "
            "друг получит за тебя награду.\n\n"
            f"🥚 Тебе уже выдан первый орёл — <b>{starter}</b>. Раз в сутки он "
            "приносит яйцо — сливай их на поле и получай Meat или новых орлов.\n"
            f"💎 Открывай слоты, покупай новых и собери всех {total_monsters} существ.\n"
            "👥 Зови своих друзей — за них тоже платят.\n\n"
            "Ферма ждёт 👇"
        )
    else:
        text = (
            "🐲 Добро пожаловать в <b>SkyLords GRAMM</b>!\n\n"
            f"🥚 Тебе уже выдан первый орёл — <b>{starter}</b>. Раз в сутки он "
            "приносит яйцо — сливай их на поле и получай Meat или новых орлов.\n"
            f"💎 Открывай слоты, покупай новых и собери всех {total_monsters} существ.\n"
            "👥 Зови друзей — за каждого дают награду.\n\n"
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
