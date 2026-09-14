import os
import json
import random
import re
import time
import asyncio
from typing import List, Optional

import httpx

from auth import verify_init_data
from storage import make_store

from fastapi import FastAPI, Header, HTTPException
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

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
# Задавать вручную не обязательно: при старте бота имя берётся через getMe.
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@").strip()

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

MISSIONS = {m["id"]: m for m in CONFIG["missions"]}
QUALIFY_MNSTR = CONFIG["referral"].get("qualify_mnstr", 10)
MONSTERS = {m["id"]: m for tier in CONFIG["tiers"] for m in tier["monsters"]}
STARTER_MONSTER = CONFIG["tiers"][0]["monsters"][0]["id"]
START_SLOTS = CONFIG["slots"]["start"]
PAYOUT_SECONDS = CONFIG["roll"]["payout_hours"] * 3600
MAX_SLOTS = CONFIG["slots"]["max"]
DAILY = CONFIG.get("daily") or {}
DAILY_DAYS = int(DAILY.get("days", 30))
DAILY_STEP = float(DAILY.get("mnstr_step", 10))
DAILY_SPECIAL = {int(item["day"]): item for item in DAILY.get("special", [])}

WHEEL = CONFIG.get("wheel") or {}
WHEEL_SEGMENTS = WHEEL.get("segments") or []

TON = CONFIG.get("ton") or {}
TON_RATE = float(TON.get("rate", 1))          # сколько GRAM даёт 1 TON
MIN_DEPOSIT = float(TON.get("min_deposit", 1))
MIN_WITHDRAW = float(TON.get("min_withdraw", 1))
MEMO_PREFIX = str(TON.get("memo_prefix", "MG"))

# Кошелёк проекта — получатель пополнений. Без него раздел кошелька выключен.
TON_WALLET = os.getenv("TON_WALLET", "").strip()
TON_API = os.getenv("TON_API_URL", "https://toncenter.com/api/v3").rstrip("/")
TON_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
TON_POLL_SECONDS = int(os.getenv("TON_POLL_SECONDS", "30"))
# Куда слать заявки на вывод: свой Telegram-id или id канала.
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()


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
def monster_rate(monster_id: str) -> float:
    """Coins per second, so a monster hands over its whole payout in payout_hours."""
    monster = MONSTERS.get(monster_id)
    return monster["payout"] / PAYOUT_SECONDS if monster else 0.0


def read_farm(raw) -> List[dict]:
    """The farm is one slot per monster: {"id": ..., "mined": coins paid out so far}.

    Farms saved in older shapes - {id: copies}, or a flat list of ids - are
    converted into fresh slots.
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
            entry = {"id": entry, "mined": 0.0}
        if not isinstance(entry, dict):
            continue
        monster_id = entry.get("id")
        if monster_id not in MONSTERS:
            continue
        payout = MONSTERS[monster_id]["payout"]
        try:
            mined = min(max(float(entry.get("mined") or 0.0), 0.0), payout)
        except (TypeError, ValueError):
            mined = 0.0
        if mined < payout:
            farm.append({"id": monster_id, "mined": mined})
    return farm


def remaining_payout(slot: dict) -> float:
    return max(0.0, MONSTERS[slot["id"]]["payout"] - slot["mined"])


def total_income(farm: List[dict]) -> float:
    """Only slots that still owe coins are mining."""
    return sum(monster_rate(slot["id"]) for slot in farm if remaining_payout(slot) > 0)


def run_farm(farm: List[dict], seconds: float):
    """Mines for `seconds`, never paying a monster beyond its payout.

    Returns (coins, mnstr). Meat is farmed in parallel over the same
    lifetime, so it accrues in step with the coin payout.
    """
    coins = 0.0
    mnstr = 0.0
    for slot in farm:
        monster = MONSTERS[slot["id"]]
        gain = min(monster_rate(slot["id"]) * seconds, remaining_payout(slot))
        if gain > 0:
            slot["mined"] += gain
            coins += gain
            mnstr += gain / monster["payout"] * monster.get("mnstr", 0.0)
    return coins, mnstr


def offline_reward(row):
    """Mines what the farm earned while the player was away.

    Returns (coins, mnstr, farm); the farm comes back with its slots advanced
    and monsters that finished their payout removed.
    """
    farm = read_farm(row["monsters"])
    last_seen = int(row["last_seen"] or 0)
    if not last_seen:
        return 0.0, 0.0, farm

    elapsed = min(max(int(time.time()) - last_seen, 0), CONFIG["offline"]["max_seconds"])
    if elapsed <= 0:
        return 0.0, 0.0, farm

    coins, mnstr = run_farm(farm, elapsed * CONFIG["offline"]["efficiency"])
    return coins, mnstr, [slot for slot in farm if remaining_payout(slot) > 0]


# --- DAILY CHECK-IN (mirrored by the client in index.html) ---
def day_index(moment: Optional[float] = None) -> int:
    """Порядковый номер суток UTC — по нему считаем серию входов."""
    return int((moment if moment is not None else time.time()) // 86400)


def daily_reward(day: int) -> dict:
    """Награда за day-й день серии: Meat по нарастающей, кроме особых дней."""
    special = DAILY_SPECIAL.get(day)
    if special:
        return {
            "gram": float(special.get("gram") or 0.0),
            "mnstr": float(special.get("mnstr") or 0.0),
            "monster": special.get("monster"),
        }
    return {"gram": 0.0, "mnstr": day * DAILY_STEP, "monster": None}


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
    }


# --- КОЛЕСО ФОРТУНЫ ---
def _wheel_reward(index: int) -> dict:
    seg = WHEEL_SEGMENTS[index]
    return {
        "index": index,
        "gram": float(seg.get("gram") or 0),
        "mnstr": float(seg.get("mnstr") or 0),
        "monster": seg.get("monster"),
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
            "monsters": [{"id": STARTER_MONSTER, "mined": 0.0}],
            "active_slot": 0,
            "missions": [],
            "slots": START_SLOTS,
            "referrals": 0,
            "referred_by": referred_by,
            "last_seen": int(time.time()),
            "daily_day": 0,
            "daily_last": 0,
            "eggs_board": [0] * 9,
            "eggs_board_unlocked": 1,
            "wallet": "",
            "ops": 0,
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
        if await store.credit_deposit(item["hash"], item["user_id"], gram, item["ts"]):
            credited += 1
            await tg_send(
                item["user_id"],
                f"✅ Пополнение зачислено: <b>{gram:g} GRAM</b> "
                f"({item['ton']:g} TON).",
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
    monsters: List[dict]       # one slot per monster: {"id", "mined"}
    active_slot: int = 0
    missions: list = []
    slots: int = START_SLOTS
    eggs_board: List[int] = []
    eggs_board_unlocked: int = 1
    ops: int = -1              # версия баланса, полученная при последней загрузке


class MissionClaim(BaseModel):
    user_id: int
    mission_id: str


class DailyClaim(BaseModel):
    user_id: int


class WheelSpin(BaseModel):
    user_id: int


class DepositCheck(BaseModel):
    user_id: int


class WalletSave(BaseModel):
    user_id: int
    address: str = ""


class WithdrawRequest(BaseModel):
    user_id: int
    address: str
    amount: float


# --- FASTAPI SETUP ---
app = FastAPI(title="Monster Gram")
app.mount("/assets", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="assets")


@app.get("/", response_class=HTMLResponse)
async def serve_webapp():
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/game_config.json")
async def serve_config():
    return FileResponse(CONFIG_PATH, media_type="application/json")


@app.get("/api/load/{user_id}")
async def load_user_data(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Loads the farm and pays out everything the monsters earned offline."""
    user_id = authenticate(x_telegram_init_data, user_id)

    context = signed_context(x_telegram_init_data)
    if context.get("start_param"):
        await attach_referrer(user_id, context["name"], context["start_param"])
    elif context.get("name"):
        await ensure_user(user_id, name=context["name"])

    row = await fetch_user(user_id)
    earned_offline, mnstr_offline, farm = offline_reward(row)

    coins = float(row.get("coins") or 0.0) + earned_offline
    total_earned = float(row.get("total_earned") or 0.0) + earned_offline
    mnstr = float(row.get("mnstr") or 0.0) + mnstr_offline

    await store.update(
        user_id,
        {
            "coins": coins,
            "total_earned": total_earned,
            "mnstr": mnstr,
            "monsters": farm,
            "last_seen": int(time.time()),
        },
    )

    return {
        "user_id": user_id,
        "coins": coins,
        "total_earned": total_earned,
        "mnstr": mnstr,
        "monsters": farm,
        "active_slot": int(row.get("active_slot") or 0),
        "missions": row.get("missions") or [],
        "slots": int(row.get("slots") or START_SLOTS),
        "referrals": int(row.get("referrals") or 0),
        "referrals_qualified": await store.count_referrals(user_id, QUALIFY_MNSTR),
        "invited_by": await inviter_name(row.get("referred_by")),
        "daily": daily_state(row),
        "eggs_board": row.get("eggs_board") or [0] * 9,
        "eggs_board_unlocked": int(row.get("eggs_board_unlocked") or 1),
        "wallet": row.get("wallet") or "",
        "ops": int(row.get("ops") or 0),
        "ton": ton_info(user_id),
        "operations": await store.recent_operations(user_id),
        "offline_earned": earned_offline,
        "offline_mnstr": mnstr_offline,
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

    eggs_board = list(state.eggs_board or [])[:9]
    eggs_board += [0] * (9 - len(eggs_board))
    eggs_board_unlocked = max(1, min(9, int(state.eggs_board_unlocked or 1)))

    await store.update(
        user_id,
        {
            "coins": state.coins,
            "total_earned": state.total_earned,
            "mnstr": state.mnstr,
            "monsters": read_farm(state.monsters),
            "active_slot": state.active_slot,
            "slots": state.slots,
            "eggs_board": eggs_board,
            "eggs_board_unlocked": eggs_board_unlocked,
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
    reward = daily_reward(day)

    # Монстра некуда селить — открываем под него слот, чтобы награда не пропала.
    extra_slot = False
    if reward["monster"]:
        if reward["monster"] not in MONSTERS:
            raise HTTPException(status_code=500, detail="Монстр награды не найден")
        slots = int(row.get("slots") or START_SLOTS)
        if len(read_farm(row["monsters"])) >= slots:
            if slots >= MAX_SLOTS:
                raise HTTPException(status_code=400, detail="Все слоты заняты — освободи один")
            extra_slot = True

    granted = await store.claim_daily(
        user_id, today, day, reward["gram"], reward["mnstr"],
        reward["monster"], extra_slot,
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
    """Крутит колесо фортуны — бесплатно и без ограничений по частоте.
    Сектор выбирает сервер, чтобы клиент не мог подделать результат."""
    if not WHEEL_SEGMENTS:
        raise HTTPException(status_code=404, detail="Колесо фортуны отключено")

    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)

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

    await store.claim_wheel(user_id, reward["gram"], reward["mnstr"], reward["monster"], extra_slot)

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "segment": reward["index"],
        "reward": {"gram": reward["gram"], "mnstr": reward["mnstr"], "monster": reward["monster"]},
        "coins": float(fresh.get("coins") or 0.0),
        "mnstr": float(fresh.get("mnstr") or 0.0),
        "total_earned": float(fresh.get("total_earned") or 0.0),
        "monsters": read_farm(fresh["monsters"]),
        "slots": int(fresh.get("slots") or START_SLOTS),
        "ops": int(fresh.get("ops") or 0),
    }


@app.get("/tonconnect-manifest.json")
async def tonconnect_manifest():
    """Манифест для TON Connect. Адрес берётся из WEB_APP_URL, чтобы не хардкодить домен."""
    base = (WEB_APP_URL or "").rstrip("/")
    return {
        "url": base,
        "name": "Monster Gram",
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

    if not await store.request_withdraw(user_id, address, amount, int(time.time())):
        raise HTTPException(status_code=400, detail="Недостаточно GRAM на балансе")

    await store.update(user_id, {"wallet": address})
    name = row.get("name") or user_id
    await tg_send(
        ADMIN_CHAT_ID,
        f"💸 Заявка на вывод\n\nИгрок: <b>{name}</b> (<code>{user_id}</code>)\n"
        f"Сумма: <b>{amount:g} GRAM</b> = {amount / TON_RATE:g} TON\n"
        f"Адрес: <code>{address}</code>",
    )
    await tg_send(
        user_id,
        f"📨 Заявка на вывод <b>{amount:g} GRAM</b> принята.\n"
        f"Выплата придёт на <code>{address}</code> после проверки.",
    )

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "ops": int(fresh.get("ops") or 0),
        "operations": await store.recent_operations(user_id),
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
            "Пока тебя не было, монстры не сидели без дела — забери намайненное."
        )
    elif invited_by:
        text = (
            f"🤝 <b>{invited_by}</b> позвал тебя в <b>Monster Gram</b>!\n\n"
            f"Теперь ты в его команде: как только намайнишь {QUALIFY_MNSTR} Meat, "
            "друг получит за тебя награду.\n\n"
            f"🥚 Тебе уже выдан первый монстр — <b>{starter}</b>. Он добывает "
            "GRAM и Meat круглосуточно, даже когда ты закрыл игру.\n"
            f"💎 Открывай слоты, покупай новых и собери всех {total_monsters} существ.\n"
            "👥 Зови своих друзей — за них тоже платят.\n\n"
            "Ферма ждёт 👇"
        )
    else:
        text = (
            "🐲 Добро пожаловать в <b>Monster Gram</b>!\n\n"
            f"🥚 Тебе уже выдан первый монстр — <b>{starter}</b>. Он добывает "
            "GRAM и Meat круглосуточно, даже когда ты закрыл игру.\n"
            f"💎 Открывай слоты, покупай новых и собери всех {total_monsters} существ.\n"
            "👥 Зови друзей — за каждого дают награду.\n\n"
            "Ферма ждёт 👇"
        )

    keyboard = [[InlineKeyboardButton("💎 Открыть Monster Gram", web_app=WebAppInfo(url=WEB_APP_URL))]]
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
    await store.init()
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
