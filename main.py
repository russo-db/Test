import os
import json
import math
import random
import re
import secrets
import time
import asyncio
import base64
import functools
import traceback
from typing import List, Optional
from urllib.parse import parse_qsl

import httpx

from auth import verify_init_data
from storage import make_store, ledger_source, LEDGER_SOURCE

# Адреса TON: tonsdk (pip install tonsdk). Если пакета на хостинге нет — та же
# нормализация встроенной реализацией (ton_address_to_friendly), результат
# идентичный: base64url, флаг bounceable, CRC16.
try:
    from tonsdk.utils import Address as TonAddress
except Exception:  # пакет не установлен — работаем без него
    TonAddress = None

from fastapi import FastAPI, Header, HTTPException, Request, Response, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --- NFT «Небесный орел» (Getgems). Официальная коллекция — единственная,
# чьи NFT дают право на еженедельный сбор Небесного Осколка (см.
# /api/nft/claim-shard). Адрес неизменен; игра NFT не минтит и не выводит,
# только читает кошелёк игрока через TON API. ---
MY_OFFICIAL_NFT_COLLECTION = "EQCNBhvUKd6Y4Y1cg565S54KqQrBnB9MMfun6QnLIPId3uLe"
# Ключ toncenter.com — уходит заголовком "X-API-Key" во ВСЕ запросы сервера к
# toncenter (проверка NFT, приём пополнений). Переменная окружения
# TONCENTER_API_KEY, если задана, имеет приоритет (см. ниже, после load_dotenv).
TONCENTER_API_KEY = "4445c80503c48492cb834ac45fa6238fb2779c511592ce7d192d40e54d0043ad"

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
def normalize_web_app_url(value: Optional[str]) -> str:
    """Telegram принимает для Mini App только https-ссылку. Если в переменной
    окружения домен записан без схемы («eaglegame.up.railway.app») или с
    http://, приводим к «https://домен/» — иначе кнопка открытия игры в боте
    падает с BadRequest «only https links are allowed»."""
    value = (value or "").strip()
    if not value:
        return ""
    value = re.sub(r"^(?:https?:)?//", "", value, flags=re.I)
    return "https://" + value.rstrip("/") + "/"


WEB_APP_URL = normalize_web_app_url(os.getenv("WEB_APP_URL"))
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
    global RESOURCE_MARKET_MIN_UNIT_PRICE
    global ARENA_SEASON_CFG, ARENA_SEASON_DAYS, ARENA_SEASON_REWARDS
    global REFERRAL_SHARE, MAX_EGG_LEVEL
    global MAINTENANCE, MAINTENANCE_ENABLED, MAINTENANCE_MESSAGE, MAINTENANCE_CHAT_URL
    global EGGS_CFG, EGG_INTERVAL_HOURS, UNLOCK_PRICES
    global HATCH_COMMON_BY_LEVEL, HATCH_COMMON_DEFAULT, HATCH_MEAT_MIN, HATCH_MEAT_MAX
    global HATCH_JACKPOT_CHANCE, HATCH_JACKPOT_MEAT_BY_LEVEL
    global FEED_BASE_COST, FEED_GROWTH, FEED_TAPS_PER_LEVEL, MERGE_COST_MEAT, MERGE_COST_GRAM, ROULETTE_BY_TIER
    global MERGE_COST_MEAT_BY_TIER
    global EXPEDITIONS_CFG, EXPEDITION_DURATION_HOURS, EXPEDITION_COST_MEAT, EXPEDITION_GOLD_BY_TIER
    global SLOTS_PRICES, VIP_TIERS
    global NEST_CFG, NEST_SHARD_PRICE_GRAM, NEST_SHARD_COOLDOWN_HOURS, NEST_PARTICLE_INTERVAL_HOURS
    global NEST_CRAFT_COST_PARTICLES, NEST_CRAFT_COST_GOLD, NEST_UPGRADE_GROUP, NEST_GRADES, NEST_GRADE_BONUS
    global NEST_ITEM_TYPES, NEST_TYPE_ORDER, NEST_SHARD_DAILY_LIMIT
    global COMBAT_BASE_STATS, CLAN_CFG, CLAN_CREATE_COST_GRAM, CLAN_CREATE_COST_GOLD, CLAN_CREATE_COST_MEAT
    global CLAN_MEMBER_LIMIT, CLAN_INITIAL_OPEN_SLOTS, CLAN_SLOT_PRICE_GRAM, CLAN_BURN_POWER_BY_TIER
    global CLAN_ROSTER_SIZE

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
    # Цена попытки слияния в Meat по ТЕКУЩЕЙ редкости пары (индекс = редкость
    # исходных орлов): Обычный→Необычный 75, →Редкий 125, →Эпический 200,
    # →Легендарный 300, →Мифический 500. Нет записи — merge_cost_meat.
    MERGE_COST_MEAT_BY_TIER = [float(v) for v in (FUSION_CFG.get("merge_cost_meat_by_tier") or [])]
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
    # Минимум за ОДНУ штуку (напр. Небесный Осколок — не дешевле 3 GRAM за
    # осколок): минимальная цена лота = max(min_price, min_price_per_unit * amount).
    RESOURCE_MARKET_MIN_UNIT_PRICE = {
        k: float(v) for k, v in (RESOURCE_MARKET_CFG.get("min_price_per_unit") or {}).items()
    }
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
    CLAN_ROSTER_SIZE = int(CLAN_CFG.get("roster_size", 10))


apply_config(load_config())

# Кошелёк проекта — получатель пополнений. Без него раздел кошелька выключен.
TON_WALLET = os.getenv("TON_WALLET", "").strip()
# TON API для чтения NFT на кошельке игрока (tonapi.io v2). Ключ не обязателен —
# без него работает публичный лимит запросов; с ключом (TONAPI_KEY) лимиты выше.
# Только MAINNET: коллекция «Небесный орел» существует в основной сети TON,
# в тестнете её нет. Адрес из переменной окружения, указывающий на testnet,
# игнорируется (см. _mainnet_url).
TONAPI_MAINNET = "https://tonapi.io"
TONCENTER_MAINNET = "https://toncenter.com"
TONAPI_URL = os.getenv("TONAPI_URL", TONAPI_MAINNET).rstrip("/")
TONCENTER_URL = os.getenv("TONCENTER_URL", TONCENTER_MAINNET).rstrip("/")
# Ключи API (необязательны). Без них работают публичные лимиты; при ошибках
# 429/401 вставьте бесплатный ключ в переменные окружения хостинга:
#   TONAPI_KEY        — ключ tonapi.io (tonconsole.com), уходит как "Authorization: Bearer <ключ>"
#   TONCENTER_API_KEY — ключ toncenter.com (Telegram-бот @tonapibot), уходит как "X-API-Key: <ключ>"
TONAPI_KEY = os.getenv("TONAPI_KEY", "").strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip() or TONCENTER_API_KEY
NFT_CLAIM_INTERVAL_DEFAULT = 7 * 24 * 3600   # боевой режим: раз в 7 дней
NFT_CLAIM_INTERVAL_MIN = 60                   # для тестов можно поставить хоть 1 минуту
NFT_CLAIM_INTERVAL_MAX = 365 * 24 * 3600
NFT_CHECK_CACHE_SECONDS = 60                  # результат проверки кошелька кешируется, чтобы не долбить TON API
TON_API = os.getenv("TON_API_URL", "https://toncenter.com/api/v3").rstrip("/")
TON_API_KEY = TONCENTER_API_KEY   # тот же ключ для приёма пополнений (toncenter v3)
TON_POLL_SECONDS = int(os.getenv("TON_POLL_SECONDS", "30"))
# Тестовый режим сервера: пока IS_PRODUCTION_MODE не включён (переменная
# окружения IS_PRODUCTION_MODE=true), в админке работает кнопка
# «[Админ-Тест] Сократить все таймеры до 1 минуты». В бою — включить.
IS_PRODUCTION_MODE = os.getenv("IS_PRODUCTION_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
# Аукцион «Раздача Небесных орлов»: старт от 10 Gram, шаг +1 Gram к ставке
# лидера, в таблице — ТОП-5 (они и забирают лоты по окончании таймера).
AUCTION_MIN_BID = 10.0
AUCTION_BID_STEP = 1.0
AUCTION_TOP_SIZE = 5
AUCTION_DEFAULT_TITLE = "Раздача Небесных орлов"
AUCTION_DURATION_MIN = 60
AUCTION_DURATION_MAX = 30 * 24 * 3600
AUCTION_SETTLE_GRACE = 3   # сек после закрытия: даём долететь ставкам «в последнюю секунду»
AUCTION_WORKER_SECONDS = 5
# Антиснайпер: ставка, сделанная, когда до конца меньше 5 минут, продлевает
# лот ровно до 05:00 — забрать лот «на последней секунде» нельзя.
AUCTION_ANTISNIPE_SECONDS = 5 * 60
AUCTION_TEST_TIMER_SECONDS = 5 * 60 + 30   # [Админ-Тест] таймер 05:30
# Что разыгрывается: каждый из ТОП-5 получает reward_amount единиц награды.
AUCTION_REWARD_TYPES = {
    "nft": "NFT Карточка",
    "sky_shards": "Небесный осколок",
    "gold": "Золото",
    "meat": "Мясо",
}
AUCTION_REWARD_MAX = {"nft": 100, "sky_shards": 1000, "gold": 1_000_000, "meat": 1_000_000}
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


# Владельцы проекта: только их Telegram ID, подтверждённый подписью initData
# (HMAC-SHA256 от токена бота), может включать/выключать техработы, менять
# список тестеров и интервал сбора осколка по NFT. Пароля админки для этих
# действий мало — админку нужно открыть через бота командой /admin (тогда
# Telegram подписывает запросы). Эти же ID всегда пускаются в игру во время
# техработ.
MAINTENANCE_WHITELIST = [6233536571, 5637579704, 827725395]
OWNER_ONLY_DETAIL = ("Только для владельцев проекта: откройте админку через бота командой /admin "
                     "(действие подтверждается подписью Telegram)")


def verified_telegram_id(request: Request) -> Optional[int]:
    """Telegram ID из X-Telegram-Init-Data — ТОЛЬКО по проверенной подписи,
    независимо от режима разработки: без BOT_TOKEN владельцем не стать."""
    try:
        data = verified_init_data(request.headers.get("x-telegram-init-data"))
        return int(data["user"]["id"]) if data else None
    except Exception:
        return None


def require_owner(request: Request):
    """Пароль админки + подпись Telegram владельца из MAINTENANCE_WHITELIST."""
    require_admin(request)
    uid = verified_telegram_id(request)
    if uid is None or uid not in MAINTENANCE_WHITELIST:
        print(f"[admin] отказ в действии владельца: telegram_id={uid} path={request.url.path}")
        raise HTTPException(status_code=403, detail=OWNER_ONLY_DETAIL)
    return uid


# Перебор пароля админки: не больше ADMIN_LOGIN_MAX_FAILS неверных попыток за
# ADMIN_LOGIN_WINDOW с одного адреса и ADMIN_LOGIN_GLOBAL_MAX со всех вместе
# (адрес за прокси Railway можно подделать заголовком, общий потолок — нет).
ADMIN_LOGIN_WINDOW = 15 * 60
ADMIN_LOGIN_MAX_FAILS = 5
ADMIN_LOGIN_GLOBAL_MAX = 30
ADMIN_LOGIN_FAILS: dict = {}   # ключ адреса -> [unix-время неудачных попыток]


def admin_client_key(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "?")


def admin_login_blocked(key: str, now: float) -> bool:
    for k in list(ADMIN_LOGIN_FAILS):
        ADMIN_LOGIN_FAILS[k] = [t for t in ADMIN_LOGIN_FAILS[k] if now - t < ADMIN_LOGIN_WINDOW]
        if not ADMIN_LOGIN_FAILS[k]:
            ADMIN_LOGIN_FAILS.pop(k)
    total = sum(len(v) for v in ADMIN_LOGIN_FAILS.values())
    return len(ADMIN_LOGIN_FAILS.get(key, [])) >= ADMIN_LOGIN_MAX_FAILS or total >= ADMIN_LOGIN_GLOBAL_MAX


store = make_store()

# Каждый игровой запрос обязан нести X-Telegram-Init-Data с подписью
# Telegram (HMAC-SHA256 от токена бота, см. auth.verify_init_data). Сервер
# закрыт по умолчанию: если BOT_TOKEN не задан, проверить подпись нечем, и
# ВСЕ игровые запросы получают 401 — а не открываются кому угодно, как было
# раньше. Открытый режим без подписи — только для локальной разработки и
# тестов, явным ALLOW_INSECURE_DEV_AUTH=1 в окружении (на Railway его быть
# не должно).
ALLOW_INSECURE_DEV_AUTH = os.getenv("ALLOW_INSECURE_DEV_AUTH", "").strip() == "1"
AUTH_REQUIRED = bool(BOT_TOKEN) or not ALLOW_INSECURE_DEV_AUTH
if not BOT_TOKEN:
    print("[auth] ВНИМАНИЕ: BOT_TOKEN не задан — " + (
        "ALLOW_INSECURE_DEV_AUTH=1: игровой API работает БЕЗ проверки подписи (только для разработки!)"
        if not AUTH_REQUIRED else "игровой API отклоняет все запросы (401), пока токен не задан"))


def verified_init_data(init_data: Optional[str]) -> Optional[dict]:
    """Проверенная initData или None — исключения проверки наружу не идут."""
    try:
        return verify_init_data(init_data or "", BOT_TOKEN or "")
    except Exception as e:
        print(f"[auth] ошибка проверки initData: {type(e).__name__}: {e}")
        return None


def authenticate(init_data: Optional[str], claimed_id: int) -> int:
    """Возвращает настоящий user_id из подписанных Telegram данных.

    telegram_id из тела/пути запроса НЕ доверяется: он лишь должен совпасть
    с id из проверенной подписи. Нет подписи / подпись не сходится / строка
    изменена — 401; подпись чужого игрока под чужим user_id — 403."""
    if not AUTH_REQUIRED:
        return claimed_id

    data = verified_init_data(init_data)
    if not data:
        raise HTTPException(status_code=401, detail="Некорректная подпись initData")
    try:
        verified_id = int(data["user"]["id"])
        claimed = int(claimed_id)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Некорректная подпись initData")
    if verified_id != claimed:
        print(f"[auth] подмена telegram_id: подпись {verified_id}, в запросе {claimed!r}")
        raise HTTPException(status_code=403, detail="initData принадлежит другому пользователю")
    return verified_id


def signed_context(init_data: Optional[str]) -> dict:
    """Подписанные поля запуска: имя игрока и реферальная нагрузка."""
    data = verified_init_data(init_data) if AUTH_REQUIRED else None
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
        slot = {
            "id": monster_id,
            "next_egg_at": next_egg_at,
            "feed_level": feed_level,
            "feed_taps": feed_taps,
            "expedition_until": max(0, expedition_until),
        }
        # Замок рынка: орёл выставлен на продажу и стоит в своей ячейке до
        # покупки или снятия лота (см. store.create_listing). Отметку нужно
        # сохранять при любой перезаписи фермы.
        listing_id = entry.get("listing_id")
        if isinstance(listing_id, str) and listing_id:
            slot["listing_id"] = listing_id
        farm.append(slot)
    return farm


def slot_listed(slot: dict) -> bool:
    return bool(slot.get("listing_id"))


def slot_on_expedition(slot: dict) -> bool:
    """В экспедиции — с момента отправки и до сбора награды."""
    return int(slot.get("expedition_until") or 0) > 0


def ensure_slot_not_listed(slot: dict) -> None:
    if slot_listed(slot):
        raise HTTPException(status_code=400, detail="Орёл выставлен на рынок — сначала сними лот")


def usable_eagle_ids(farm: List[dict]) -> set:
    """Виды орлов, которыми игрок может пользоваться (Арена, клан): орёл,
    выставленный на рынок, ничего не делает."""
    return {m["id"] for m in farm if not slot_listed(m)}


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


def merge_cost_meat(tier_idx: int) -> float:
    """Meat за одну попытку слияния пары орлов редкости tier_idx."""
    if 0 <= tier_idx < len(MERGE_COST_MEAT_BY_TIER):
        return MERGE_COST_MEAT_BY_TIER[tier_idx]
    return MERGE_COST_MEAT


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
    arena_fight/arena_buy_energy для трат)."""
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


def ledger_labelled(source: str):
    """Метка для журнала балансов у начислений, которые идут не от действия
    игрока, а «попутно» или в фоне (VIP-мясо, пополнения TON, награды сезона
    Арены) — иначе в журнале был бы адрес запроса, внутри которого это случилось."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            with ledger_source(source):
                return await fn(*args, **kwargs)
        return wrapper
    return deco


@ledger_labelled("vip:meat")
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


async def record_economy(**amounts) -> None:
    """Статистика экономики для админки (вкладка «Экономика»): куда уходит
    золото (крафт / мясо у купца / кланы / энергия Арены), сколько его добыто
    в экспедициях и чем оплачивают скрещивание — мясом или GRAM. Сбой записи
    статистики никогда не ломает само действие игрока."""
    try:
        await store.record_economy(amounts, time.strftime("%Y-%m-%d", time.gmtime()))
    except Exception as e:
        print(f"[economy] record failed: {type(e).__name__}: {e}")


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


# Расписание сезона хранится в БД (arena_season: season, started_at, ends_at,
# days) — его можно менять из админки. Раньше номер сезона считался формулой
# «время ÷ длительность», и смена длительности перескакивала номер сезона:
# короче — сезон мгновенно заканчивался с раздачей наград, длиннее — смена
# не наступала очень долго. Здесь — кэш документа (обновляет
# load_arena_season, вызывается на каждом /api/load через reconcile).
ARENA_SEASON_STATE: dict = {}
ARENA_SEASON_CACHE_SECONDS = 30
_arena_season_loaded_at = 0.0


def legacy_arena_season_doc(now: float) -> dict:
    """Сезон по старой формуле — для перехода: текущий сезон продолжается
    с тем же номером и тем же временем окончания, что видели игроки."""
    season = arena_season_index(now)
    return {"season": season, "started_at": float(season * ARENA_SEASON_DAYS * 86400),
            "ends_at": float(arena_season_ends_at(season)), "days": ARENA_SEASON_DAYS}


async def load_arena_season(now: Optional[float] = None, force: bool = False) -> dict:
    global _arena_season_loaded_at
    now = time.time() if now is None else now
    if (not force and ARENA_SEASON_STATE and now - _arena_season_loaded_at < ARENA_SEASON_CACHE_SECONDS
            and now < ARENA_SEASON_STATE["ends_at"]):
        return ARENA_SEASON_STATE
    doc = await store.get_arena_season()
    if not doc or doc.get("ends_at") is None:
        seed = legacy_arena_season_doc(now)
        if doc and doc.get("season") is not None:
            seed["season"] = int(doc["season"])
        doc = await store.init_arena_season(seed)
    ARENA_SEASON_STATE.clear()
    ARENA_SEASON_STATE.update({
        "season": int(doc["season"]), "started_at": float(doc["started_at"]),
        "ends_at": float(doc["ends_at"]), "days": int(doc.get("days") or ARENA_SEASON_DAYS),
    })
    _arena_season_loaded_at = now
    return ARENA_SEASON_STATE


def arena_season_view() -> dict:
    """Текущий сезон Арены для клиента — из кэша расписания (его обновляет
    reconcile_arena_season); до первой загрузки — по старой формуле."""
    if ARENA_SEASON_STATE:
        return {"season": ARENA_SEASON_STATE["season"], "ends_at": ARENA_SEASON_STATE["ends_at"]}
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
        if message.get("bounced"):
            continue                      # вернувшийся наш же перевод, а не пополнение
        description = tx.get("description") or {}
        compute = description.get("compute_ph") or {}
        if description.get("aborted") or compute.get("success") is False:
            continue                      # транзакция не прошла — TON проект не получил
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


# Повторное зачисление отсекается хэшем транзакции в коллекции deposits.
# Если базу очистили, этот список пропадает, а TON API по-прежнему отдаёт
# последние 100 переводов на кошелёк — и все старые пополнения зачислились бы
# второй раз. Поэтому сервер помнит отметку времени (settings →
# deposits_since): на свежей/очищенной базе (ни одного депозита и нет
# отметки) она ставится на момент первой проверки, и переводы старше неё не
# зачисляются. На работающей базе отметка = 0 и ничего не меняется.
DEPOSITS_SINCE_SETTING = "deposits_since"
_deposits_since: Optional[float] = None


async def deposits_since() -> float:
    global _deposits_since
    if _deposits_since is not None:
        return _deposits_since
    saved = await store.get_setting(DEPOSITS_SINCE_SETTING, None)
    if saved is None:
        saved = 0.0 if await store.count_deposits() else float(int(time.time()))
        await store.set_setting(DEPOSITS_SINCE_SETTING, saved)
        if saved:
            print(f"[ton] новая/очищенная база: пополнения раньше {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(saved))} не зачисляются")
    _deposits_since = float(saved)
    return _deposits_since


@ledger_labelled("ton:deposit")
async def _scan_deposits() -> int:
    try:
        incoming = await fetch_incoming()
        since = await deposits_since()
    except Exception as error:
        print(f"TON: не удалось получить транзакции: {error}")
        return 0

    credited = 0
    for item in incoming:
        if item["ts"] < since:
            continue                      # перевод старше отметки — из истории до очистки базы
        if item["ton"] + 1e-9 < MIN_DEPOSIT:
            # Меньше минимального пополнения — не зачисляем. Запоминаем перевод,
            # чтобы не обрабатывать его снова, и один раз сообщаем игроку.
            await ensure_user(item["user_id"])
            if await store.record_below_min_deposit(item["hash"], item["user_id"], item["ton"], item["ts"]):
                print(f"[ton] перевод {item['ton']:g} TON от {item['user_id']} меньше минимума {MIN_DEPOSIT:g} TON — не зачислен")
                await tg_send(
                    item["user_id"],
                    f"⚠️ Перевод <b>{item['ton']:g} TON</b> меньше минимального пополнения "
                    f"<b>{MIN_DEPOSIT:g} TON</b> и не зачислен. Напишите в поддержку игры.",
                )
            continue
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


class FeedAction(BaseModel):
    """Тап кормления. Кроме номера слота клиент присылает, КАКОГО орла он
    видит в этом слоте (вид + уровень + прогресс тапов): если на сервере в
    слоте уже другой орёл или другой прогресс (список фермы сдвинулся,
    ответ пришёл не по порядку, кормили с другого устройства) — тап
    отклоняется 409, а не уходит «не тому» орлу."""
    user_id: int
    slot_index: int
    monster_id: Optional[str] = None
    feed_level: Optional[int] = None
    feed_taps: Optional[int] = None


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


class ArenaFightRequest(BaseModel):
    user_id: int
    tier_id: str
    match_token: str


class ArenaBuyEnergy(BaseModel):
    user_id: int
    currency: str  # "gold" | "gram"


class MarketListRequest(BaseModel):
    user_id: int
    monster_id: str
    price_gram: float
    slot_index: Optional[int] = None  # конкретная ячейка фермы; None — первая подходящая


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


class NftClaimRequest(BaseModel):
    user_id: int


class AdminNftSettings(BaseModel):
    interval_seconds: int


class AdminNftCollection(BaseModel):
    address: str = ""


class AuctionBidRequest(BaseModel):
    user_id: int
    auction_id: str
    amount: Optional[float] = None   # ставка, которую игрок видел на экране


class AdminAuctionCreate(BaseModel):
    title: str = AUCTION_DEFAULT_TITLE
    item_image: str = ""
    description: str = ""
    duration_seconds: int = 24 * 3600
    min_bid: float = AUCTION_MIN_BID
    reward_type: str = "nft"          # nft | sky_shards | gold | meat
    reward_amount: float = 1          # сколько получает КАЖДЫЙ победитель


class AdminAuctionAction(BaseModel):
    auction_id: str


class AdminAuctionDelivery(BaseModel):
    auction_id: str
    user_id: int
    delivered: bool = True


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


class AdminClanRename(BaseModel):
    name: str


class AdminClanSetSlots(BaseModel):
    open_slots: int


class AdminClanKick(BaseModel):
    user_id: int


class AdminCreateBotClan(BaseModel):
    name: str
    clan_power: float
    member_count: int = 1


class AdminBulkCreateBotClans(BaseModel):
    count: int
    min_power: float = 100
    max_power: float = 5000
    member_count: int = 10


class AdminClanTournamentStart(BaseModel):
    size: int
    match_minute: Optional[int] = None  # время матчей, минуты от полуночи UTC; None — CLAN_MATCH_HOUR_UTC


class AdminClanTournamentMatchTime(BaseModel):
    match_minute: int  # минуты от полуночи UTC (0..1439) — для всех ещё не сыгранных матчей


class AdminClanMatchTime(BaseModel):
    start_time: Optional[float] = None  # абсолютное время (эпоха, сек); None — вернуть расписание турнира
    now: bool = False  # открыть матч прямо сейчас (по серверным часам)


# --- FASTAPI SETUP ---
app = FastAPI(title="SkyLords GRAMM")
app.mount("/assets", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="assets")

# Пока включены техработы, все /api/* эндпоинты (кроме самой проверки статуса)
# отвечают 503 — админка и статика (страница, конфиг, ассеты) продолжают
# работать как обычно, чтобы экран техработ на клиенте мог загрузиться.
# /api/merchant/state — только чтение; его же показывает админка (блок «Купец»),
# и во время техработ он не должен падать с 503.
MAINTENANCE_ALLOWED_PATHS = {"/api/maintenance", "/api/merchant/state"}


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Непойманная ошибка сервера. Полный traceback — в лог хостинга. Админке
    (/admin/api/...) отдаём ещё и тип/текст ошибки, чтобы вместо голого
    «Ошибка 500» было видно, что именно сломалось; игровой API деталей
    наружу не раскрывает."""
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    if request.url.path.startswith("/admin/api/"):
        detail = f"Ошибка сервера: {type(exc).__name__}: {exc}"[:500]
    else:
        detail = "Внутренняя ошибка сервера"
    return JSONResponse(status_code=500, content={"detail": detail})


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    """Тело запроса не прошло проверку типов. Стандартный ответ FastAPI
    возвращает присланные значения обратно, а NaN/Infinity (json.loads их
    пропускает) в JSON не сериализуются — вместо 422 получался 500. Отдаём
    только поле и причину, без самих значений."""
    errors = [{"loc": list(e.get("loc", [])), "msg": str(e.get("msg", ""))} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


class LedgerSourceMiddleware:
    """Источник для журнала балансов = адрес запроса (/api/farm/expedition/collect,
    /admin/api/players/123 …). Чистый ASGI, без BaseHTTPMiddleware, чтобы
    contextvar гарантированно был виден внутри обработчика."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        token = LEDGER_SOURCE.set(scope.get("path") or "http")
        try:
            return await self.app(scope, receive, send)
        finally:
            LEDGER_SOURCE.reset(token)


app.add_middleware(LedgerSourceMiddleware)


# Режим техработ и список тестеров живут в БД (settings → maintenance_state),
# а не в game_config.json: файл конфига Railway восстанавливает из репозитория
# при каждом деплое, и включённые техработы сами выключались бы ровно в тот
# момент, когда выкатывается обновление. Пока в БД состояния нет — действует
# maintenance.enabled из конфига.
MAINTENANCE_SETTING = "maintenance_state"   # {"enabled": bool, "testers": [Telegram ID, ...]}
MAINTENANCE_REFRESH_SECONDS = 10            # другие копии сервера подхватят смену за 10 с
MAINTENANCE_TESTERS_MAX = 50
_maintenance_db_enabled: Optional[bool] = None
MAINTENANCE_TESTERS: set = set()
_maintenance_loaded_at = 0.0


def maintenance_on() -> bool:
    return MAINTENANCE_ENABLED if _maintenance_db_enabled is None else _maintenance_db_enabled


def parse_tester_ids(raw) -> list:
    out = []
    for x in raw or []:
        try:
            uid = int(x)
        except (TypeError, ValueError):
            continue
        if uid > 0 and uid not in out:
            out.append(uid)
    return out[:MAINTENANCE_TESTERS_MAX]


async def refresh_maintenance(force: bool = False) -> None:
    """Подтягивает режим техработ и тестеров из БД (не чаще раза в 10 с).
    Сбой базы — остаёмся на последнем известном состоянии."""
    global _maintenance_db_enabled, MAINTENANCE_TESTERS, _maintenance_loaded_at
    now = time.time()
    if not force and now - _maintenance_loaded_at < MAINTENANCE_REFRESH_SECONDS:
        return
    _maintenance_loaded_at = now
    try:
        doc = await store.get_setting(MAINTENANCE_SETTING, None)
        if isinstance(doc, dict):
            _maintenance_db_enabled = bool(doc["enabled"]) if "enabled" in doc else None
            MAINTENANCE_TESTERS = set(parse_tester_ids(doc.get("testers")))
    except Exception as e:
        print(f"[maintenance] не удалось прочитать состояние из БД: {type(e).__name__}: {e}")


async def save_maintenance_state(enabled: Optional[bool] = None, testers: Optional[list] = None) -> dict:
    doc = await store.get_setting(MAINTENANCE_SETTING, None)
    doc = dict(doc) if isinstance(doc, dict) else {}
    if enabled is not None:
        doc["enabled"] = bool(enabled)
    if testers is not None:
        doc["testers"] = parse_tester_ids(testers)
    await store.set_setting(MAINTENANCE_SETTING, doc)
    await refresh_maintenance(force=True)
    return {"maintenance_enabled": maintenance_on(), "testers": sorted(MAINTENANCE_TESTERS)}


def request_telegram_id(request: Request) -> Optional[int]:
    """Telegram ID из заголовка X-Telegram-Init-Data — только по проверенной
    подписи бота. Без BOT_TOKEN (локальная разработка) подпись не проверить,
    поэтому ID берётся из initData как есть — как и во всём остальном API."""
    init_data = request.headers.get("x-telegram-init-data") or ""
    if not init_data:
        return None
    try:
        if AUTH_REQUIRED:
            data = verified_init_data(init_data)
            return int(data["user"]["id"]) if data else None
        user = json.loads(dict(parse_qsl(init_data, keep_blank_values=True)).get("user") or "null")
        return int(user["id"]) if isinstance(user, dict) and user.get("id") else None
    except Exception:
        return None


def is_maintenance_tester(request: Request) -> bool:
    uid = request_telegram_id(request)
    return uid is not None and (uid in MAINTENANCE_TESTERS or uid in MAINTENANCE_WHITELIST)


@app.middleware("http")
async def admin_api_gate(request: Request, call_next):
    """/admin/api/*: запрос с подписью Telegram (то есть из игры) пропускаем
    только для владельцев из MAINTENANCE_WHITELIST — обычный игрок получает
    жёсткий 403, даже не дойдя до проверки пароля. Без подписи — обычная
    проверка сессии админки (401 без входа)."""
    try:
        if request.url.path.startswith("/admin/api/") and request.headers.get("x-telegram-init-data"):
            uid = verified_telegram_id(request)
            if uid is None or uid not in MAINTENANCE_WHITELIST:
                print(f"[admin] 403: telegram_id={uid} пытался вызвать {request.url.path}")
                return JSONResponse(status_code=403, content={"detail": "Доступ запрещён"})
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        return JSONResponse(status_code=403, content={"detail": "Доступ запрещён"})
    return await call_next(request)


# Раздел «Кланы» можно выключить из админки (вкладка «Кланы»). Флаг в БД
# (settings → clans_enabled), чтобы деплой его не сбрасывал. Выключено —
# все /api/clan/* отвечают 403 и кнопка «Кланы» в игре скрыта; владельцы
# (MAINTENANCE_WHITELIST) и тестеры техработ видят раздел, чтобы проверять.
# Фоновые процессы (турнир кланов) при этом не останавливаются.
CLANS_SETTING = "clans_enabled"
CLANS_REFRESH_SECONDS = 10
CLANS_DISABLED_DETAIL = "Раздел кланов временно отключён"
_clans_enabled = True
_clans_loaded_at = 0.0


async def refresh_clans_enabled(force: bool = False) -> None:
    global _clans_enabled, _clans_loaded_at
    now = time.time()
    if not force and now - _clans_loaded_at < CLANS_REFRESH_SECONDS:
        return
    _clans_loaded_at = now
    try:
        _clans_enabled = bool(await store.get_setting(CLANS_SETTING, True))
    except Exception as e:
        print(f"[clans] не удалось прочитать флаг раздела: {type(e).__name__}: {e}")


def clans_open_for(request: Request) -> bool:
    return _clans_enabled or is_maintenance_tester(request)


async def clans_enabled_for(request: Request) -> bool:
    try:
        await refresh_clans_enabled()
        return clans_open_for(request)
    except Exception:
        return True


@app.middleware("http")
async def clans_gate(request: Request, call_next):
    try:
        if request.url.path.startswith("/api/clan/"):
            await refresh_clans_enabled()
            if not clans_open_for(request):
                return JSONResponse(status_code=403, content={"detail": CLANS_DISABLED_DETAIL, "clans_disabled": True})
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
    return await call_next(request)


@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    """В режиме техработ /api/* отвечает 503 всем, кроме тестеров из списка
    в админке — они играют как обычно и проверяют обновление."""
    if request.url.path.startswith("/api/") and request.url.path not in MAINTENANCE_ALLOWED_PATHS:
        await refresh_maintenance()
        if maintenance_on() and not is_maintenance_tester(request):
            return JSONResponse(status_code=503, content={"detail": MAINTENANCE_MESSAGE})
    return await call_next(request)


@app.get("/health")
async def health():
    """Диагностика для владельца (открыть в браузере): жив ли сервер,
    отвечает ли MongoDB, проходит ли запись и сколько места на её диске.
    Каждая проверка с таймаутом — страница отвечает, даже если база лежит."""
    report = {"server": "ok", "time": int(time.time())}
    db = store.settings.database

    async def step(name, coro, timeout=5):
        started = time.time()
        try:
            value = await asyncio.wait_for(coro, timeout)
            report[name] = {"ok": True, "ms": int((time.time() - started) * 1000)}
            return value
        except Exception as e:
            report[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
            return None

    await step("db_ping", db.command("ping"))
    await step("db_write", store.settings.update_one({"_id": "health"}, {"$set": {"value": time.time()}}, upsert=True))
    await step("db_read_player", store.users.find_one({}, projection={"_id": 1}))
    stats = await step("db_stats", db.command("dbStats"))
    if stats:
        mb = lambda v: round(float(v or 0) / 1024 / 1024, 1)
        report["disk_mb"] = {
            "data": mb(stats.get("dataSize")), "storage": mb(stats.get("storageSize")),
            "indexes": mb(stats.get("indexSize")),
            "fs_used": mb(stats.get("fsUsedSize")), "fs_total": mb(stats.get("fsTotalSize")),
            "fs_free": mb(float(stats.get("fsTotalSize") or 0) - float(stats.get("fsUsedSize") or 0)),
        }
    report["status"] = "ok" if all(report[k]["ok"] for k in ("db_ping", "db_write", "db_read_player")) else "DATABASE PROBLEM"
    return report


@app.get("/api/maintenance")
async def maintenance_status(request: Request):
    """enabled=false для тестера — его клиент грузит игру как обычно;
    tester=true — чтобы показать ему плашку «идут техработы»."""
    await refresh_maintenance()
    on = maintenance_on()
    tester = on and is_maintenance_tester(request)
    return {
        "enabled": on and not tester,
        "tester": tester,
        "message": MAINTENANCE_MESSAGE,
        "chat_url": MAINTENANCE_CHAT_URL,
    }


@app.get("/", response_class=HTMLResponse)
async def serve_webapp():
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/game_config.json")
async def serve_config():
    """Конфиг для клиента: файл + задания и их вкл/выкл из БД (админка)."""
    try:
        await refresh_missions()
        cfg = load_config()
        cfg["missions"] = [{k: m.get(k) for k in ("id", "title", "type", "gram", "mnstr", "url") if k in m}
                           for m in missions_list()]
        cfg["missions_enabled"] = missions_on()
        return JSONResponse(cfg, headers={"Cache-Control": "no-store"})
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        return FileResponse(CONFIG_PATH, media_type="application/json")


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    with open(os.path.join(BASE_DIR, "admin.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/load/{user_id}")
async def load_user_data(user_id: int, request: Request, x_telegram_init_data: Optional[str] = Header(None)):
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
    # Имя пишется при создании аккаунта, но аккаунт мог появиться раньше —
    # из параллельного запроса при загрузке, зачисления депозита, как
    # пригласивший по реферальной ссылке — и остаться без имени (в игре и
    # админке вместо ника был Telegram ID). Обновляем из подписанной initData
    # при каждом входе: пустое или сменившееся имя/@username.
    if context.get("name") and (row.get("name") or "") != context["name"]:
        try:
            await store.update(user_id, {"name": context["name"]})
            row["name"] = context["name"]
        except Exception as e:
            print(f"[load] не удалось обновить имя {user_id}: {type(e).__name__}: {e}")
    row = await accrue_vip_meat(user_id, row)
    row = await reconcile_queues(user_id, row)
    row = await reconcile_pvp_energy(user_id, row)
    row = await reconcile_nest_particles(user_id, row)
    farm = read_farm(row["monsters"])

    # CAS, не голый store.update: между чтением row выше и этой записью
    # игрок мог успеть покормить орла/собрать яйцо в параллельном запросе
    # (тапы идут своим CAS-путём — см. run_farm_action), и наивная
    # перезапись monsters здесь стёрла бы этот параллельный тап. При
    # конфликте просто пропускаем миграцию формы фермы в БД на этот раз —
    # ничего не портит: read_farm() и так нормализует её на каждом чтении,
    # это лишь ленивая персистентная миграция старых сохранений.
    for _ in range(3):
        ops = int(row.get("ops") or 0)
        fields = {"monsters": farm, "last_seen": int(time.time())}
        if await store.cas_update(user_id, fields, ops):
            row = dict(row)
            row.update(fields)
            row["ops"] = ops + 1
            break
        row = await fetch_user(user_id)
        farm = read_farm(row["monsters"])

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
        "clans_enabled": await clans_enabled_for(request),
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
    # Выбор орла на сцене — чисто визуальный и балансов не трогает, поэтому
    # сохраняется всегда. Раньше при несовпадении ops (а каждый тап кормления
    # его двигает) сервер отвечал «stale», выбор не сохранялся, клиент
    # перечитывал состояние и экран «сам» переключался на старого орла.
    farm_len = len(read_farm(row.get("monsters")))
    active_slot = max(0, min(int(state.active_slot), max(0, farm_len - 1)))
    await store.update(user_id, {"active_slot": active_slot, "last_seen": int(time.time())})
    return {"status": "success", "ops": server_ops, "active_slot": active_slot}


# --- FARM/EGGS/VIP ACTIONS: атомарные, сервер сам считает и проверяет всё,
# клиент только шлёт намерение (какой слот/индекс) и показывает ответ. ---

@app.post("/api/farm/feed")
async def farm_feed(request: FeedAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Тап кормления: списывает Meat, продвигает прогресс тапов; на
    feed_taps_per_level тапов запускает таймер яйца. Всё — по свежему
    документу из БД внутри CAS (run_farm_action): баланс мяса и уровень/
    прогресс орла берутся из базы, а не из запроса; данные из запроса
    служат только проверкой, что клиент кормит того орла, которого видит."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    def compute(row):
        farm = read_farm(row.get("monsters"))
        i = request.slot_index
        if not (0 <= i < len(farm)):
            raise HTTPException(status_code=404, detail="Слот не найден")
        slot = farm[i]
        if request.monster_id is not None and slot["id"] != request.monster_id:
            raise HTTPException(status_code=409, detail="stale: в этом слоте уже другой орёл")
        if request.feed_level is not None and slot["feed_level"] != int(request.feed_level):
            raise HTTPException(status_code=409, detail="stale: уровень орла изменился")
        if request.feed_taps is not None and slot["feed_taps"] != int(request.feed_taps):
            raise HTTPException(status_code=409, detail="stale: прогресс кормления изменился")
        ensure_slot_not_listed(slot)
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
        return fields, {"mnstr": mnstr, "slot": slot, "slot_index": i, "started_farming": started_farming,
                        "monster_id": slot["id"]}

    try:
        return await run_farm_action(user_id, compute)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[feed] user={user_id} slot={request.slot_index} monster={request.monster_id} "
              f"level={request.feed_level} taps={request.feed_taps}: {type(e).__name__}: {e}")
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail="Не удалось покормить орла — попробуйте ещё раз")


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
        ensure_slot_not_listed(slot)
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
        ensure_slot_not_listed(a)
        ensure_slot_not_listed(b)
        if slot_on_expedition(a) or slot_on_expedition(b):
            raise HTTPException(status_code=400, detail="Орёл в экспедиции")
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
        cost = MERGE_COST_GRAM if use_gram else merge_cost_meat(idx)   # редкость берётся из БД, не из запроса
        coins = float(row.get("coins") or 0)
        mnstr = float(row.get("mnstr") or 0)
        if use_gram:
            if coins < cost:
                raise HTTPException(status_code=400, detail="Не хватает GRAM")
            coins -= cost
        else:
            if mnstr < cost:
                raise HTTPException(status_code=400, detail=f"Не хватает Meat: попытка слияния стоит {cost:g} Meat")
            mnstr -= cost

        seg_i = pick_weighted_index(segments)
        outcome = segments[seg_i]
        slots_count = int(row.get("slots") or START_SLOTS)
        farm_queue = read_farm(row.get("farm_queue"))[:FARM_QUEUE_MAX]

        fields = {"coins": coins, "mnstr": mnstr}
        response = {
            "outcome": outcome.get("type"), "segment_index": seg_i,
            "coins": coins, "mnstr": mnstr, "cost": cost, "currency": "gram" if use_gram else "meat",
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

    result = await run_farm_action(user_id, compute)
    if request.use_gram:
        await record_economy(fusion_tries_gram=1, gram_fusion=MERGE_COST_GRAM)
    else:
        await record_economy(fusion_tries_meat=1, meat_fusion=float(result.get("cost") or MERGE_COST_MEAT))
    return result


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
        ensure_slot_not_listed(slot)
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
        ensure_slot_not_listed(slot)
        if slot["expedition_until"] <= 0 or time.time() < slot["expedition_until"]:
            raise HTTPException(status_code=400, detail="Экспедиция ещё не вернулась")

        reward = expedition_gold_reward(slot["id"])
        slot["expedition_until"] = 0
        gold = max(0.0, float(row.get("gold") or 0)) + reward
        fields = {"monsters": farm, "gold": gold}
        return fields, {"slot": slot, "slot_index": i, "gold": gold, "reward": reward}

    result = await run_farm_action(user_id, compute)
    await record_economy(gold_expeditions=result["reward"], expeditions=1)
    return result


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
        ensure_slot_not_listed(farm[i])
        if slot_on_expedition(farm[i]):
            raise HTTPException(status_code=400, detail="Орёл в экспедиции — его нельзя удалить")
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
    в пределах ±ARENA_LADDER_RATING_RANGE очков рейтинга. Если рядом никого
    нет — матч всё равно выдаётся, просто is_bot=true (дикий орёл; см.
    arena_fight, который и сгенерирует его статы, когда бой реально начнётся).

    КРИТИЧНО ДЛЯ БЕЗОПАСНОСТИ: на этом шаге отдаётся ТОЛЬКО округлённый до
    сотен рейтинг и одноразовый match_token — ни ник, ни точный рейтинг, ни
    редкость орла, ни снаряжение соперника клиенту не передаются. Иначе
    точная цифра рейтинга однозначно вычисляла бы конкретного игрока в
    Топ-100 ещё ДО начала боя. Сам бой и его итог — тоже не на этом шаге:
    единственная точка, где что-либо реально решается — /api/arena/fight,
    вызываемый клиентом строго в момент запуска анимации (см. arena_fight)."""
    user_id = authenticate(x_telegram_init_data, user_id)
    row = await fetch_user(user_id)
    my_rating = pvp_rating_of(row)

    opponent = await store.find_ladder_opponent(
        user_id, my_rating, ARENA_LADDER_ABOVE_COUNT, ARENA_LADDER_RATING_RANGE,
    )

    _prune_arena_matches()
    token = secrets.token_urlsafe(16)
    ARENA_PENDING_MATCHES[token] = {
        "user_id": user_id,
        "opponent_id": opponent["user_id"] if opponent else None,
        "expires_at": time.time() + ARENA_MATCH_TTL_SECONDS,
        # Случайность боя (и статы дикого орла) — от этого секрета, который
        # клиенту не отдаётся. Раньше посевом служил сам match_token, его
        # клиент знает ДО боя — можно было заранее просчитать исход и
        # драться только в заведомо выигрышных боях.
        "seed": secrets.token_hex(16),
    }
    return {
        "found": True,
        "is_bot": opponent is None,
        "match_token": token,
        "rating_bracket": rating_bracket(opponent["pvp_rating"]) if opponent else None,
    }


def _arena_bot_stats(rng: random.Random, tier_id: str) -> dict:
    """Дикий орёл-заглушка — порт клиентского arenaGenerateBotOpponent:
    базовые статы редкости, которой сражается игрок, ±15% каждая независимо.
    Раньше это подставлял себе сам клиент безо всякого сервера; теперь для
    авторитетного боя сервер обязан сгенерировать те же числа сам — иначе
    игрок мог бы просто заявить о победе над ботом, которого сам же ослабил."""
    base = COMBAT_BASE_STATS.get(tier_id) or {"hp": 100, "atk": 10, "def": 8, "crit": 5, "spd": 10}
    v = lambda: 0.85 + rng.random() * 0.3
    return {
        "hp": round(base["hp"] * v()), "atk": round(base["atk"] * v()),
        "def": round(base["def"] * v()), "crit": round(base["crit"] * v(), 1),
        "spd": round(base["spd"] * v()),
    }


def arena_battle_simulate(seed: str, player: dict, enemy: dict) -> dict:
    """1v1 бой Арены — порт клиентского arenaStartBattle/arenaRollDamage,
    теперь единственный источник истины для исхода (раньше бой полностью
    отыгрывался на клиенте, а сервер лишь принимал заявленный им won —
    см. историю /api/arena/result). player/enemy — {"stats"}; кто ходит
    первым решает скорость (при равной — 50/50), дальше стороны меняются
    ходом за ходом, как и в клиентском цикле. Лог из одних только "attack"
    (без "enter" — фигурант ровно один на сторону, замены нет, в отличие от
    очереди 10х10 в Битве Кланов) клиент проигрывает как анимацию, так что
    визуал 1-в-1 совпадает с тем, что реально произошло."""
    rng = random.Random(seed)
    a_hp, b_hp = player["stats"]["hp"], enemy["stats"]["hp"]
    a_stats, b_stats = player["stats"], enemy["stats"]

    if a_stats["spd"] > b_stats["spd"]:
        attacker_side = "player"
    elif b_stats["spd"] > a_stats["spd"]:
        attacker_side = "enemy"
    else:
        attacker_side = "player" if rng.random() < 0.5 else "enemy"

    log = []
    guard = 0
    while a_hp > 0 and b_hp > 0 and guard < 2000:
        guard += 1
        attacker_stats = a_stats if attacker_side == "player" else b_stats
        defender_stats = b_stats if attacker_side == "player" else a_stats
        dmg, crit = _clan_roll_damage(rng, attacker_stats, defender_stats)
        if attacker_side == "player":
            b_hp = max(0, b_hp - dmg)
            defender_hp = b_hp
        else:
            a_hp = max(0, a_hp - dmg)
            defender_hp = a_hp
        log.append({"side": attacker_side, "value": dmg, "crit": crit, "defender_hp": defender_hp})
        attacker_side = "enemy" if attacker_side == "player" else "player"

    return {"winner": "player" if a_hp > 0 else "enemy", "log": log}


@app.post("/api/arena/fight")
async def arena_fight(request: ArenaFightRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Единственная точка, где реально решается бой Арены. Раньше это были
    три независимых, ничем не связанных друг с другом вызова:
    /api/arena/spend_energy списывал энергию, клиент сам отыгрывал бой у
    себя в JS, а /api/arena/result просто ЗАПИСЫВАЛ заявленный клиентом
    won — ни один из них не проверял, что остальные два действительно
    произошли. Это позволяло накрутить PvP-рейтинг (а через сезонные
    награды — и реальный GRAM) прямыми запросами без единого настоящего
    боя. Теперь: одноразовый match_token из /api/arena/opponent обязателен
    и сгорает при первом использовании (анти-повтор), энергия проверяется и
    списывается сервером в том же атомарном шаге, что и рейтинг (см.
    run_farm_action), а исход считает arena_battle_simulate — тем же
    портом клиентской формулы урона, что и Битва Кланов, только 1 на 1."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    if request.tier_id not in TIER_INDEX:
        raise HTTPException(status_code=400, detail="Неизвестная редкость")

    row = await fetch_user(user_id)
    if request.tier_id not in usable_eagle_ids(read_farm(row.get("monsters"))):
        raise HTTPException(status_code=400, detail="Нет орла этой редкости")

    match = ARENA_PENDING_MATCHES.pop(request.match_token, None)
    if not match or match["user_id"] != user_id or match["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="Соперник устарел — попробуй ещё раз")

    equipped = normalize_nest_equipped(row.get("nest_equipped"))
    player_fighter = {"stats": combat_eagle_stats(request.tier_id, equipped.get(request.tier_id))}

    opponent_id = match.get("opponent_id")
    is_bot = opponent_id is None
    seed = match.get("seed") or secrets.token_hex(16)
    if is_bot:
        enemy_stats = _arena_bot_stats(random.Random(seed), request.tier_id)
        enemy_name, enemy_tier_id = None, request.tier_id
    else:
        opponent_row = await store.get(opponent_id)
        if not opponent_row:
            raise HTTPException(status_code=400, detail="Соперник уже недоступен")
        enemy_tier_id = best_owned_tier(opponent_row.get("monsters"))
        enemy_equipped = normalize_nest_equipped(opponent_row.get("nest_equipped")).get(enemy_tier_id, {})
        enemy_stats = combat_eagle_stats(enemy_tier_id, enemy_equipped)
        enemy_name = opponent_row.get("name") or f"Игрок {opponent_id}"
    enemy_fighter = {"stats": enemy_stats}

    result = arena_battle_simulate(seed, player_fighter, enemy_fighter)
    player_won = result["winner"] == "player"

    def compute(fresh_row):
        energy, day = pvp_energy_of(fresh_row)
        if energy < PVP_ENERGY_COST:
            raise HTTPException(status_code=400, detail="Нет энергии")
        energy -= PVP_ENERGY_COST
        new_rating = max(0, pvp_rating_of(fresh_row) + (PVP_RATING_WIN if player_won else -PVP_RATING_LOSS))
        fields = {"pvp_energy": energy, "pvp_energy_day": day, "pvp_rating": new_rating}
        extra = {
            "won": player_won, "pvp_rating": new_rating, "pvp_energy": energy,
            "energy_reset_at": arena_energy_reset_at(day), "battle_log": result["log"],
            "player": {"stats": player_fighter["stats"]},
            "enemy": {"name": enemy_name, "tier_id": enemy_tier_id, "is_bot": is_bot, "stats": enemy_stats},
        }
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

    result = await run_farm_action(user_id, compute)
    if request.currency == "gold":
        await record_economy(gold_arena=ARENA_ENERGY_PRICE_GOLD)
    else:
        await record_economy(gram_arena=ARENA_ENERGY_PRICE_GRAM)
    return result


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
    # arena_fight — построенный на той же идее). limit=None — в таблицу
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


@ledger_labelled("arena:season_rewards")
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

        # CAS по ops, а не голый store.update: награда — это ПРИБАВКА к тому,
        # что уже было (row.coins + reward), а не абсолютное значение, поэтому
        # если между чтением и записью игрок успел что-то потратить/получить
        # сам, наивная перезапись стёрла бы это параллельное изменение. При
        # конфликте перечитываем и складываем награду заново поверх свежего
        # состояния — тот же принцип, что и в accrue_vip_meat/reconcile_queues.
        for _ in range(3):
            ops = int(row.get("ops") or 0)
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

            if await store.cas_update(user_id, fields, ops):
                awarded.append({"user_id": user_id, "rank": rank, **reward})
                break
            row = await store.get(user_id)
            if not row:
                break

    return {"rewarded": len(awarded), "details": awarded}


async def reconcile_arena_season() -> None:
    """Проверяет, не закончился ли текущий сезон Арены (ends_at из БД —
    длительность меняется в админке). Если закончился, ровно ОДИН из
    конкурентных вызовов выигрывает store.advance_arena_season (conditional
    update по season+ends_at) и только он раздаёт призы Топ-50 уходящего
    сезона (distribute_arena_rewards — строго ДО сброса рейтинга), затем
    сбрасывает PvP-рейтинг всем к PVP_RATING_START. Пока сезон идёт — no-op
    по кэшу состояния (ARENA_SEASON_CACHE_SECONDS). Сбой здесь только
    логируется: смена сезона повторится при следующем вызове, а вход в игру
    и Таблица лидеров из-за него не ломаются."""
    try:
        now = time.time()
        state = await load_arena_season(now)
        if now < state["ends_at"]:
            return
        days = int(state["days"])
        # Новый сезон начинается ровно в момент конца старого; если сервер
        # простоял дольше целого сезона — с текущего момента.
        new_started = state["ends_at"] if now < state["ends_at"] + days * 86400 else now
        if await store.advance_arena_season(state["season"], state["ends_at"], new_started,
                                            new_started + days * 86400, days):
            print(f"[arena] сезон {state['season']} завершён — раздаём награды Топ-50 и сбрасываем рейтинг")
            await distribute_arena_rewards()
            await store.reset_all_pvp_ratings(PVP_RATING_START)
        await load_arena_season(now, force=True)
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)


class AdminArenaSeason(BaseModel):
    days: int
    apply_to_current: bool = True


@app.post("/admin/api/arena/season")
async def admin_arena_season(body: AdminArenaSeason, _: None = Depends(require_admin)):
    """Длительность сезона Арены. apply_to_current — пересчитать конец
    ТЕКУЩЕГО сезона от его начала (начало + N дней); иначе новая длительность
    действует со следующего сезона. Если новый конец уже в прошлом, сезон
    завершается сразу: награды Топ-50 и сброс рейтинга."""
    try:
        days = int(body.days)
        if not 1 <= days <= 365:
            raise HTTPException(status_code=400, detail="Длительность сезона — от 1 до 365 дней")
        now = time.time()
        state = await load_arena_season(now, force=True)
        new_ends = state["started_at"] + days * 86400 if body.apply_to_current else None
        if new_ends is not None and new_ends <= now:
            new_ends = now   # сезон уже «просрочен» — заканчиваем сейчас, следующий стартует с этого момента
        await store.set_arena_season_schedule(days, new_ends)
        print(f"[admin] сезон Арены: {days} дн."
              + (f", конец текущего → {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(new_ends))}" if new_ends else " со следующего сезона"))
        ended = bool(new_ends is not None and new_ends <= now)
        await load_arena_season(now, force=True)
        if ended:
            await reconcile_arena_season()
        state = await load_arena_season(force=True)
        return {"season": state["season"], "started_at": state["started_at"], "ends_at": state["ends_at"],
                "season_days": state["days"], "ended_now": ended}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось изменить сезон: {type(e).__name__}: {e}")


@app.post("/admin/api/arena/distribute_rewards")
async def admin_distribute_arena_rewards(_: None = Depends(require_admin)):
    """Ручной запуск начисления призов турнира Арены по текущему Топ-50 —
    жми из админки по факту окончания турнира (см. distribute_arena_rewards).
    Места/рейтинг игроков этим не сбрасываются."""
    try:
        return await distribute_arena_rewards()
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось раздать награды: {type(e).__name__}: {e}")


class AdminArenaPlayer(BaseModel):
    user_id: int
    pvp_rating: Optional[float] = None
    pvp_energy: Optional[float] = None


@app.get("/admin/api/arena")
async def admin_arena(search: str = "", limit: int = 100, _: None = Depends(require_admin)):
    """Админка → Арена: сезон, правила, награды по местам, таблица лидеров
    (с поиском) с энергией каждого игрока и призом, который он получит."""
    try:
        limit = max(1, min(500, int(limit)))
        rows = await store.arena_admin_list(search, limit)
        players = []
        for i, r in enumerate(rows, start=1):
            place = i if not search.strip() else await store.count_higher_rating(r["pvp_rating"]) + 1
            energy, _day = pvp_energy_of(r)
            reward = arena_tournament_reward(place) if place <= 50 else {"gram": 0, "shards": 0, "particles": 0}
            players.append({
                "place": place, "user_id": r["user_id"], "name": r["name"], "pvp_rating": r["pvp_rating"],
                "pvp_energy": energy, "last_seen": r.get("last_seen"), "reward": reward,
            })
        state = await load_arena_season(force=True)
        eco = (await store.get_economy(days=1)).get("total") or {}
        return {
            "season": state["season"], "ends_at": state["ends_at"], "season_days": state["days"],
            "started_at": state["started_at"],
            "server_time": time.time(), "rewards": ARENA_SEASON_REWARDS,
            "rating": {"start": PVP_RATING_START, "win": PVP_RATING_WIN, "loss": PVP_RATING_LOSS},
            "energy": {"max": PVP_ENERGY_MAX, "cost": PVP_ENERGY_COST,
                       "price_gold": ARENA_ENERGY_PRICE_GOLD, "price_gram": ARENA_ENERGY_PRICE_GRAM,
                       "reset_at": arena_energy_reset_at(day_index())},
            "economy": {"gold_spent": float(eco.get("gold_arena") or 0), "gram_spent": float(eco.get("gram_arena") or 0)},
            "total_players": await store.count_arena_players(),
            "players": players, "search": search,
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось загрузить Арену: {type(e).__name__}: {e}")


@app.post("/admin/api/arena/player")
async def admin_arena_player(body: AdminArenaPlayer, _: None = Depends(require_admin)):
    """Поправить игроку рейтинг Арены и/или энергию (энергия ставится на
    сегодня — суточное пополнение её не перезапишет до следующих суток)."""
    try:
        fields = {}
        if body.pvp_rating is not None:
            if not 0 <= float(body.pvp_rating) <= 1_000_000:
                raise HTTPException(status_code=400, detail="Рейтинг — от 0 до 1 000 000")
            fields["pvp_rating"] = float(body.pvp_rating)
        if body.pvp_energy is not None:
            if not 0 <= float(body.pvp_energy) <= 1000:
                raise HTTPException(status_code=400, detail="Энергия — от 0 до 1000")
            fields.update({"pvp_energy": float(body.pvp_energy), "pvp_energy_day": day_index()})
        if not fields:
            raise HTTPException(status_code=400, detail="Нечего менять")
        if not await store.admin_set_arena_player(body.user_id, fields):
            raise HTTPException(status_code=404, detail="Игрок не найден")
        print(f"[admin] Арена: игроку {body.user_id} установлено {fields}")
        return {"status": "ok", "user_id": body.user_id, **fields}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить: {type(e).__name__}: {e}")


@app.post("/admin/api/arena/reset_ratings")
async def admin_arena_reset_ratings(_: None = Depends(require_admin)):
    """Сбросить рейтинг Арены ВСЕМ игрокам до стартового (как при смене
    сезона, но без раздачи наград — их раздают отдельной кнопкой)."""
    try:
        await store.reset_all_pvp_ratings(PVP_RATING_START)
        print(f"[admin] Арена: рейтинг всех игроков сброшен до {PVP_RATING_START}")
        return {"status": "ok", "rating": PVP_RATING_START}
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось сбросить рейтинг: {type(e).__name__}: {e}")


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


class ClanKickRequest(BaseModel):
    user_id: int
    member_id: int


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


# Турнир кланов сам ботов/NPC не генерирует — участвуют только кланы, уже
# существующие на сервере, состав замораживается один раз в момент ручного запуска
# админом (см. /admin/api/clan_tournament/start). Один игровой день ==
# ровно 24 реальных часа (IS_PRODUCTION_MODE на клиенте — index.html).
CLAN_TOURNAMENT_SIZES = (8, 16, 32)
# Матчи по умолчанию открываются в это время (UTC) каждого турнирного дня —
# см. clan_match_start_time. Админ может сменить время матчей всего турнира
# (поле match_minute документа турнира) или перенести отдельный матч
# (start_time_override матча) — см. clan_tournament_match_time.
CLAN_MATCH_HOUR_UTC = 20
CLAN_MATCH_MINUTE_DEFAULT = CLAN_MATCH_HOUR_UTC * 60
# Цепочка раундов на вылет от старшего к младшему и их параметры (сколько
# матчей в раунде и сколько матчей в день) — тот же костяк раскладки,
# что и в изначальной фиксированной Топ-32 сетке, просто сетка меньшего
# масштаба (8/16) начинается не с r32, а сразу с соответствующего звена
# цепочки (см. clan_round_specs_for_size).
CLAN_ROUND_CHAIN = ["r32", "r16", "r8", "r4"]
CLAN_ROUND_MATCH_INFO = {"r32": (16, 2), "r16": (8, 1), "r8": (4, 1), "r4": (2, 1)}


def clan_round_specs_for_size(size: int) -> tuple:
    """Раунды плей-офф для выбранного масштаба турнира (8/16/32) —
    список (round_key, число_матчей, день_начала, матчей_в_день) плюс
    день матча за 3-е место. Раскладка размера n начинается прямо со
    звена 'r{n}' цепочки CLAN_ROUND_CHAIN — раунд «r16» устроен ОДИНАКОВО
    и в турнире на 32 клана (как второй раунд), и в турнире на 16 (как
    первый), так что вся арифметика дней ниже переиспользуется как есть."""
    start = CLAN_ROUND_CHAIN.index(f"r{size}")
    specs, day = [], 1
    for round_key in CLAN_ROUND_CHAIN[start:]:
        match_count, per_day = CLAN_ROUND_MATCH_INFO[round_key]
        specs.append((round_key, match_count, day, per_day))
        day += -(-match_count // per_day)  # ceil division
    return specs, day  # day == день матча за 3-е место; день финала — day+1


def clan_round_offsets(size: int) -> dict:
    """Индекс первого матча каждого раунда в плоском списке bracket и
    число матчей в нём — производится от size, не хранится отдельно."""
    specs, day_3rd = clan_round_specs_for_size(size)
    offsets, idx = {}, 0
    for round_key, match_count, _, _ in specs:
        offsets[round_key] = (idx, match_count)
        idx += match_count
    offsets["r3rd"] = (idx, 1)
    offsets["final"] = (idx + 1, 1)
    return offsets


def clan_match_start_time(start_at: float, day: int, match_minute: int = CLAN_MATCH_MINUTE_DEFAULT) -> float:
    """Абсолютное серверное время открытия матча по расписанию турнира —
    полночь дня запуска (start_at) + (day-1) полных суток + match_minute
    минут (по умолчанию CLAN_MATCH_HOUR_UTC часов)."""
    return start_at + (day - 1) * 86400 + match_minute * 60


def clan_tournament_match_time(tournament: dict, match: dict) -> float:
    """Фактическое время открытия матча: ручной перенос админом
    (start_time_override), иначе расписание турнира с его match_minute."""
    override = match.get("start_time_override")
    if override is not None:
        return float(override)
    return clan_match_start_time(
        tournament["start_at"], match.get("day", 1),
        int(tournament.get("match_minute", CLAN_MATCH_MINUTE_DEFAULT)),
    )


def _clan_feeder_indices(size: int) -> dict:
    """Для каждого матча — индексы матчей, чьи победители/проигравшие
    заполняют его слоты. Матч не разыгрывается, пока они не сыграны, даже
    если админ перенёс его раньше них (см. _advance_clan_bracket)."""
    feeders: dict = {}
    for round_key, (offset, count) in clan_round_offsets(size).items():
        for local_idx in range(count):
            for target in (_clan_next_match_for_winner(round_key, local_idx, size),
                           _clan_next_match_for_loser(round_key, local_idx, size)):
                if target:
                    feeders.setdefault(target[0], []).append(offset + local_idx)
    return feeders


# --- «Прямой Эфир»: матч разрешается сервером мгновенно, но зрителю
# показывается в реальном времени. У каждого события battle_log есть своё
# смещение от начала эфира (clan_broadcast_schedule) — одно и то же для
# сервера и клиента; весь матч 10х10 идёт CLAN_BROADCAST_MIN..MAX секунд.
# Пока эфир идёт, игровой API не раскрывает ни победителя, ни ещё не
# «случившиеся» события лога, ни клан, прошедший дальше по сетке.
CLAN_BROADCAST_MIN_SECONDS = 90.0
CLAN_BROADCAST_MAX_SECONDS = 120.0
CLAN_BROADCAST_EVENT_SECONDS = {"enter": 0.6, "attack": 1.3, "death": 1.6}
CLAN_BROADCAST_TAIL_SECONDS = 1.5   # пауза после последнего удара до объявления победителя
CLAN_BROADCAST_LOOKAHEAD_SECONDS = 4.0  # сколько лога вперёд отдаём клиенту (он опрашивает каждые ~2 с)


def clan_broadcast_schedule(log: list) -> tuple:
    """(offsets, duration, time_scale): offsets[i] — секунда эфира, в
    которую начинается событие log[i]; duration — длина всего эфира;
    time_scale — во сколько раз «базовые» длительности событий растянуты
    (>1) или сжаты (<1), чтобы уложить матч в MIN..MAX секунд. Пустой лог
    (техническая победа) — эфира нет, duration == 0."""
    if not log:
        return [], 0.0, 1.0
    raw, t = [], 0.0
    for event in log:
        raw.append(t)
        t += CLAN_BROADCAST_EVENT_SECONDS.get(event.get("type"), 1.0)
    t += CLAN_BROADCAST_TAIL_SECONDS
    duration = min(max(t, CLAN_BROADCAST_MIN_SECONDS), CLAN_BROADCAST_MAX_SECONDS)
    scale = duration / t
    return [round(x * scale, 3) for x in raw], duration, scale


def clan_match_broadcast_window(tournament: dict, match: dict) -> Optional[tuple]:
    """(начало, конец) эфира разрешённого матча или None, если смотреть
    нечего. Начало — не раньше start_time и не раньше фактического
    разрешения (resolved_at): матч, который сервер разыграл позже своего
    времени (никто не заходил), начинается в эфире с момента разрешения."""
    if not match.get("resolved"):
        return None
    _, duration, _ = clan_broadcast_schedule(match.get("battle_log") or [])
    if duration <= 0:
        return None
    start = max(clan_tournament_match_time(tournament, match), float(match.get("resolved_at") or 0))
    return start, start + duration


def clan_tournament_view_bracket(tournament: dict, hide_live: bool = False, now: Optional[float] = None) -> list:
    """Сетка для клиента/админки — без battle_log, с абсолютным start_time
    каждого матча (см. clan_tournament_match_time). hide_live (игровой
    API): у матча, эфир которого ещё идёт, скрыт победитель (live=True,
    live_started_at/live_ends_at), а клан, уже переставленный им в слот
    следующего несыгранного матча, снова показан как «TBD» — иначе итог
    эфира был бы виден в сетке раньше, чем зритель его досмотрит."""
    now = time.time() if now is None else now
    size = tournament["size"]
    bracket = []
    for match in tournament["bracket"]:
        m = {k: v for k, v in match.items() if k != "battle_log"}
        m["start_time"] = clan_tournament_match_time(tournament, match)
        bracket.append(m)
    if not hide_live:
        return bracket

    offsets = clan_round_offsets(size)
    for idx, match in enumerate(tournament["bracket"]):
        window = clan_match_broadcast_window(tournament, match)
        if not window or now >= window[1]:
            continue
        m = bracket[idx]
        m["live"] = True
        m["live_started_at"], m["live_ends_at"] = window
        hidden_winner = m.get("winner_id")
        m["winner_id"] = m["winner_name"] = None
        local_idx = idx - offsets[match["round"]][0]
        for target in (_clan_next_match_for_winner(match["round"], local_idx, size),
                       _clan_next_match_for_loser(match["round"], local_idx, size)):
            if not target:
                continue
            target_idx, slot = target
            nxt = bracket[target_idx]
            if nxt.get("resolved") or not hidden_winner:
                continue
            nxt[f"{slot}_id"] = nxt[f"{slot}_name"] = None
    return bracket


def build_clan_bracket(clans: list, size: int) -> list:
    """Строит турнирную сетку выбранного масштаба (8/16/32 — см.
    CLAN_TOURNAMENT_SIZES) из уже существующих кланов сервера — сама ботов не
    генерирует: только первый раунд получает участников (посев по clan_power, см.
    _clan_seed_order); все последующие матчи, включая матч за 3-е место и
    финал, начинаются с пустых слотов, которые заполняются по мере
    разрешения предыдущих матчей (см. _advance_clan_bracket). Если
    кланов меньше size — недостающие места сетки первого раунда
    становятся техническими «бай» (пустой слот, автопобеда соперника).
    clans должен уже быть отфильтрован/обрезан до top-size вызывающей
    стороной (см. /admin/api/clan_tournament/start — clan_count >= size
    проверяется ДО вызова этой функции)."""
    seeds = _clan_seed_order(size)
    slots = [None] * size
    for i, seed in enumerate(seeds):
        idx = seed - 1
        slots[i] = clans[idx] if idx < len(clans) else None

    specs, day_3rd = clan_round_specs_for_size(size)
    bracket = []
    for round_idx, (round_key, match_count, day_start, per_day) in enumerate(specs):
        for i in range(match_count):
            a = slots[2 * i] if round_idx == 0 else None
            b = slots[2 * i + 1] if round_idx == 0 else None
            bracket.append({
                "round": round_key, "day": day_start + i // per_day,
                "clan_a_id": a["id"] if a else None, "clan_a_name": a["name"] if a else None,
                "clan_b_id": b["id"] if b else None, "clan_b_name": b["name"] if b else None,
                "resolved": False, "winner_id": None, "winner_name": None, "battle_log": [],
            })
    bracket.append({"round": "r3rd", "day": day_3rd, "clan_a_id": None, "clan_a_name": None,
                     "clan_b_id": None, "clan_b_name": None, "resolved": False,
                     "winner_id": None, "winner_name": None, "battle_log": []})
    bracket.append({"round": "final", "day": day_3rd + 1, "clan_a_id": None, "clan_a_name": None,
                     "clan_b_id": None, "clan_b_name": None, "resolved": False,
                     "winner_id": None, "winner_name": None, "battle_log": []})
    return bracket


def _clan_next_match_for_winner(round_key: str, local_idx: int, size: int) -> Optional[tuple]:
    """Куда попадает победитель матча round_key[local_idx] — (индекс
    следующего матча в bracket, слот 'clan_a'/'clan_b'). None — для
    финала и матча за 3-е место (дальше сетки нет)."""
    if round_key == "r4":
        return (clan_round_offsets(size)["final"][0], "clan_a" if local_idx == 0 else "clan_b")
    if round_key not in CLAN_ROUND_CHAIN:
        return None  # "r3rd"/"final" сами уже конец сетки
    chain_pos = CLAN_ROUND_CHAIN.index(round_key)
    next_key = CLAN_ROUND_CHAIN[chain_pos + 1]
    next_offset = clan_round_offsets(size)[next_key][0]
    return (next_offset + local_idx // 2, "clan_a" if local_idx % 2 == 0 else "clan_b")


def _clan_next_match_for_loser(round_key: str, local_idx: int, size: int) -> Optional[tuple]:
    """Проигравший полуфинала (round_key == 'r4') уходит в матч за 3-е
    место — единственный случай, когда исход матча кланов важен и
    победителю, и проигравшему."""
    if round_key == "r4":
        return (clan_round_offsets(size)["r3rd"][0], "clan_a" if local_idx == 0 else "clan_b")
    return None


async def _clan_roster_fighters(clan: Optional[dict]) -> list:
    """Собирает боевых бойцов клана из его approved_lineup — для каждой
    записи {user_id, tier_id} подтягивает текущее снаряжение владельца в
    Кузнице на этой редкости и считает итоговые статы (combat_eagle_stats).
    Проверяется В МОМЕНТ МАТЧА, а не при подаче/утверждении расстановки:
    в бой выходит только нынешний участник клана, у которого орёл этой
    редкости всё ещё есть на ферме. Ушедший/исключённый игрок или продавший
    (сжёгший) орла просто выпадает из состава; если не осталось никого,
    клан считается «без состава» (см. _advance_clan_bracket)."""
    if not clan:
        return []
    members = set(clan.get("members") or [])
    fighters = []
    for entry in (clan.get("approved_lineup") or [])[:CLAN_ROSTER_SIZE]:
        user_id = entry.get("user_id")
        tier_id = entry.get("tier_id")
        if tier_id not in TIER_INDEX or user_id is None or user_id not in members:
            continue
        row = await store.get(user_id)
        if not row:
            continue
        if tier_id not in usable_eagle_ids(read_farm(row.get("monsters"))):
            continue
        equipped = normalize_nest_equipped(row.get("nest_equipped"))
        fighters.append({
            "user_id": user_id, "name": row.get("name") or "",
            "tier_id": tier_id, "stats": combat_eagle_stats(tier_id, equipped.get(tier_id)),
        })
    return fighters


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
    size = tournament["size"]
    feeders = _clan_feeder_indices(size)

    for idx, match in enumerate(bracket):
        if match.get("resolved"):
            continue
        if clan_tournament_match_time(tournament, match) > now:
            continue
        if any(not bracket[f].get("resolved") for f in feeders.get(idx, [])):
            continue  # матч перенесён раньше предыдущего раунда — ждём его

        a_id, b_id = match.get("clan_a_id"), match.get("clan_b_id")
        winner_id = winner_name = None
        battle_log: list = []
        roster_sizes = {"fighters_a": 0, "fighters_b": 0}

        if a_id and b_id:
            clan_a, clan_b = await store.get_clan(a_id), await store.get_clan(b_id)
            fighters_a = await _clan_roster_fighters(clan_a)
            fighters_b = await _clan_roster_fighters(clan_b)
            roster_sizes = {"fighters_a": len(fighters_a), "fighters_b": len(fighters_b)}
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
            elif clan_a or clan_b:
                # Расстановку не подал никто (например, тестовые клан-боты) —
                # проходит сильнейший по clan_power, при равенстве — clan_a
                # (он выше посеян), чтобы сетка не застревала на «TBD».
                # Распущенный клан (get_clan → None) проигрывает существующему.
                power_a = float(clan_a.get("clan_power") or 0) if clan_a else -1.0
                power_b = float(clan_b.get("clan_power") or 0) if clan_b else -1.0
                if power_b > power_a:
                    winner_id, winner_name = b_id, match.get("clan_b_name")
                else:
                    winner_id, winner_name = a_id, match.get("clan_a_name")
        elif a_id:
            winner_id, winner_name = a_id, match.get("clan_a_name")  # технический бай
        elif b_id:
            winner_id, winner_name = b_id, match.get("clan_b_name")

        if not await store.resolve_clan_match(idx, winner_id, winner_name, battle_log, now, roster_sizes):
            continue  # уже разрешён другим конкурентным вызовом

        match["resolved"], match["winner_id"], match["winner_name"], match["battle_log"] = (
            True, winner_id, winner_name, battle_log,
        )
        match["resolved_at"] = now
        match.update(roster_sizes)

        round_key = match["round"]
        local_idx = idx - clan_round_offsets(size)[round_key][0]
        if winner_id:
            target = _clan_next_match_for_winner(round_key, local_idx, size)
            if target:
                target_idx, slot = target
                if await store.set_clan_match_participant(target_idx, slot, winner_id, winner_name):
                    bracket[target_idx][f"{slot}_id"] = winner_id
                    bracket[target_idx][f"{slot}_name"] = winner_name
        if round_key == "r4":
            loser_id = b_id if winner_id == a_id else (a_id if winner_id == b_id else None)
            loser_name = match.get("clan_b_name") if loser_id == b_id else match.get("clan_a_name")
            target = _clan_next_match_for_loser(round_key, local_idx, size)
            if target and loser_id:
                target_idx, slot = target
                if await store.set_clan_match_participant(target_idx, slot, loser_id, loser_name):
                    bracket[target_idx][f"{slot}_id"] = loser_id
                    bracket[target_idx][f"{slot}_name"] = loser_name


async def current_clan_tournament() -> Optional[dict]:
    """Текущий турнир, запущенный админом, или None. Документ старого
    автоматического (календарного) турнира без поля size считается
    отсутствующим: его сетка несовместима с ручной схемой, а новый запуск
    из админки просто перезапишет его (см. try_launch_clan_tournament)."""
    tournament = await store.get_clan_tournament()
    if not tournament or "size" not in tournament:
        return None
    return tournament


CLAN_ROSTER_LOCKED_DETAIL = (
    "Клан участвует в турнире — состав заморожен: нельзя принимать, исключать, "
    "выходить или распускать клан до выбывания или конца турнира"
)


async def clan_roster_locked(clan_id: str) -> bool:
    """Клан ещё в турнире — стоит в слоте хотя бы одного несыгранного
    матча (победитель сразу переносится в слот следующего матча, см.
    _advance_clan_bracket), значит его состав заморожен. Выбывший клан
    (проиграл и дальше по сетке не идёт) и все кланы после конца турнира
    снова свободны. Сначала продвигаем сетку, чтобы уже назревший, но ещё
    не разыгранный проигрыш не держал клан запертым."""
    await reconcile_clan_tournament()
    tournament = await current_clan_tournament()
    if not tournament:
        return False
    return any(
        not m.get("resolved") and clan_id in (m.get("clan_a_id"), m.get("clan_b_id"))
        for m in tournament["bracket"]
    )


async def ensure_clan_roster_unlocked(clan_id: str) -> None:
    if await clan_roster_locked(clan_id):
        raise HTTPException(status_code=400, detail=CLAN_ROSTER_LOCKED_DETAIL)


async def reconcile_clan_tournament() -> None:
    """Продвигает уже запущенный админом турнир (см.
    /admin/api/clan_tournament/start) — разрешает все назревшие матчи
    (_advance_clan_bracket), что дешёвый no-op, пока время очередного
    матча не наступило. Турнир НЕ запускается и не перезапускается
    автоматически — пока админ не нажмёт «Запустить Турнир», здесь просто
    нечего продвигать."""
    tournament = await current_clan_tournament()
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
        "roster_locked": await clan_roster_locked(clan_id),
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
    await record_economy(gold_clans=CLAN_CREATE_COST_GOLD, meat_clans=CLAN_CREATE_COST_MEAT, gram_clans=CLAN_CREATE_COST_GRAM)

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
    await ensure_clan_roster_unlocked(clan_id)

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


@app.post("/api/clan/kick")
async def clan_kick(request: ClanKickRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Лидер исключает участника из клана (см. kick_clan_member) —
    вышвырнутый теряет clan_id, но его личный burned_power при этом
    навсегда остаётся при нём (см. модель личной силы игрока)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")
    await ensure_clan_roster_unlocked(clan_id)

    result = await store.kick_clan_member(user_id, clan_id, request.member_id)
    if result != "ok":
        messages = {
            "not_found": "Клан не найден",
            "not_leader": "Исключать участников может только лидер клана",
            "not_member": "Этот игрок не состоит в вашем клане",
            "cannot_kick_self": "Лидер не может исключить сам себя",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось исключить участника"))

    clan = await store.get_clan(clan_id)
    return {"status": "success", "clan": clan_view(clan)}


@app.post("/api/clan/leave")
async def clan_leave(request: ClanAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Выход из клана — лидерство переходит следующему участнику, если
    выходит лидер (см. leave_clan)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")
    await ensure_clan_roster_unlocked(clan_id)

    result = await store.leave_clan(user_id, clan_id)
    if result != "ok":
        raise HTTPException(status_code=400, detail="Не удалось покинуть клан")
    return {"status": "success"}


@app.post("/api/clan/disband")
async def clan_disband(request: ClanAction, x_telegram_init_data: Optional[str] = Header(None)):
    """Лидер распускает клан целиком — клан удаляется, все участники
    (включая лидера) остаются без клана (см. disband_clan). Личный
    burned_power каждого участника при этом не теряется — он навсегда
    привязан к аккаунту, а не к клану."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    row = await fetch_user(user_id)
    clan_id = row.get("clan_id")
    if not clan_id:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")
    await ensure_clan_roster_unlocked(clan_id)

    result = await store.disband_clan(user_id, clan_id)
    if result != "ok":
        messages = {
            "not_found": "Клан не найден",
            "not_leader": "Распустить клан может только лидер",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось распустить клан"))
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
    if request.tier_id not in usable_eagle_ids(read_farm(row.get("monsters"))):
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
    """Турнирная сетка выбранного админом масштаба целиком — вкладка
    «Битва Кланов». battle_log каждого матча здесь не отдаётся (может
    быть длинным) — за ним отдельно, см. /api/clan/tournament/match/
    {match_index}. Турнир запускается ТОЛЬКО вручную админом (см.
    /admin/api/clan_tournament/start) — если он ещё не запущен, отдаём
    пустую сетку. Каждому матчу добавлен абсолютный start_time (эпоха) —
    клиент считает от него обратный отсчёт до открытия «Прямого Эфира»."""
    authenticate(x_telegram_init_data, user_id)
    await reconcile_clan_tournament()
    tournament = await current_clan_tournament()
    now = time.time()
    if not tournament:
        return {"cycle": 0, "size": 0, "start_at": 0, "bracket": [], "server_time": now}
    return {
        "cycle": tournament["cycle"], "size": tournament["size"], "start_at": tournament["start_at"],
        "bracket": clan_tournament_view_bracket(tournament, hide_live=True, now=now), "server_time": now,
    }


@app.get("/api/clan/tournament/match/{match_index}")
async def clan_tournament_match(match_index: int, user_id: int,
                                 x_telegram_init_data: Optional[str] = Header(None)):
    """Один матч турнира для «Прямого Эфира» вместе с battle_log и
    расписанием эфира (event_offsets — секунда эфира каждого события, см.
    clan_broadcast_schedule). Пока эфир идёт (live=True), отдаются только
    события, которые уже «случились» (плюс небольшой запас вперёд для
    плавности), а победитель скрыт — клиент досматривает остальное,
    периодически запрашивая этот же адрес. После эфира — весь лог целиком
    (режим повтора)."""
    authenticate(x_telegram_init_data, user_id)
    await reconcile_clan_tournament()
    tournament = await current_clan_tournament()
    bracket = (tournament or {}).get("bracket") or []
    if not (0 <= match_index < len(bracket)):
        raise HTTPException(status_code=404, detail="Матч не найден")
    now = time.time()
    view = clan_tournament_view_bracket(tournament, hide_live=True, now=now)[match_index]
    match = bracket[match_index]
    log = match.get("battle_log") or []
    offsets, duration, scale = clan_broadcast_schedule(log)
    view.update({
        "server_time": now, "broadcast_duration": duration, "time_scale": scale,
        # Размер составов на момент матча — не из лога: число «enter» у
        # победителя выдало бы, скольких бойцов ему хватило.
        "fighters_a": int(match.get("fighters_a") or 0),
        "fighters_b": int(match.get("fighters_b") or 0),
        "log_total": len(log),
    })
    window = clan_match_broadcast_window(tournament, match)
    if window:
        view["live_started_at"], view["live_ends_at"] = window
    if view.get("live"):
        visible = sum(1 for o in offsets if o <= now - window[0] + CLAN_BROADCAST_LOOKAHEAD_SECONDS)
        view["battle_log"], view["event_offsets"] = log[:visible], offsets[:visible]
    else:
        view["battle_log"], view["event_offsets"] = log, offsets
    return view


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

    result = await run_farm_action(user_id, compute)
    await record_economy(gold_craft=NEST_CRAFT_COST_GOLD, particles_craft=NEST_CRAFT_COST_PARTICLES, crafts=1)
    return result


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


# --- ЗАДАНИЯ: список редактируется в админке (вкладка «Задания») и хранится
# в БД (settings → missions_state), а не в game_config.json — файл конфига
# Railway восстанавливает из репозитория при каждом деплое, и добавленные
# в админке задания пропадали бы после любого обновления. Пока в БД списка
# нет — действуют missions из конфига. Тип channel — подписка на канал/группу,
# сервер проверяет её через бота (бот должен быть админом канала); link —
# просто открыть ссылку, награда без проверки (старый тип chat — то же самое).
MISSIONS_SETTING = "missions_state"   # {"missions": [...], "enabled": bool}
MISSIONS_REFRESH_SECONDS = 10
MISSIONS_MAX = 50
MISSION_GRAM_MAX = 100.0
MISSION_MEAT_MAX = 100_000.0
_missions_db: Optional[list] = None
_missions_db_enabled: Optional[bool] = None
_missions_loaded_at = 0.0


def missions_on() -> bool:
    return MISSIONS_ENABLED if _missions_db_enabled is None else _missions_db_enabled


def missions_list(include_hidden: bool = False) -> list:
    src = _missions_db if _missions_db is not None else list(CONFIG.get("missions") or [])
    return [m for m in src if include_hidden or m.get("active", True)]


def mission_by_id(mission_id: str) -> Optional[dict]:
    return next((m for m in missions_list() if m.get("id") == mission_id), None)


async def refresh_missions(force: bool = False) -> None:
    global _missions_db, _missions_db_enabled, _missions_loaded_at
    now = time.time()
    if not force and now - _missions_loaded_at < MISSIONS_REFRESH_SECONDS:
        return
    _missions_loaded_at = now
    try:
        doc = await store.get_setting(MISSIONS_SETTING, None)
        if isinstance(doc, dict):
            if isinstance(doc.get("missions"), list):
                _missions_db = doc["missions"]
            _missions_db_enabled = bool(doc["enabled"]) if "enabled" in doc else None
    except Exception as e:
        print(f"[missions] не удалось прочитать задания из БД: {type(e).__name__}: {e}")


async def save_missions_state(missions: Optional[list] = None, enabled: Optional[bool] = None) -> None:
    doc = await store.get_setting(MISSIONS_SETTING, None)
    doc = dict(doc) if isinstance(doc, dict) else {}
    if missions is not None:
        doc["missions"] = missions
    if enabled is not None:
        doc["enabled"] = bool(enabled)
    await store.set_setting(MISSIONS_SETTING, doc)
    await refresh_missions(force=True)


MISSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MISSION_CHAT_RE = re.compile(r"^(@[A-Za-z0-9_]{4,32}|-100\d{5,20})$")


def normalize_mission(raw: dict, index: int) -> dict:
    """Проверяет одно задание из админки. ValueError — с понятной причиной."""
    n = f"Задание №{index + 1}"
    if not isinstance(raw, dict):
        raise ValueError(f"{n}: неверный формат")
    title = str(raw.get("title") or "").strip()
    if not 1 <= len(title) <= 80:
        raise ValueError(f"{n}: название — от 1 до 80 символов")
    kind = str(raw.get("type") or "link")
    kind = "link" if kind == "chat" else kind
    if kind not in ("channel", "link"):
        raise ValueError(f"{n}: неизвестный тип задания")
    try:
        gram = float(raw.get("gram") or 0)
        meat = float(raw.get("mnstr") or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{n}: награда должна быть числом")
    if not (math.isfinite(gram) and 0 <= gram <= MISSION_GRAM_MAX):
        raise ValueError(f"{n}: GRAM — от 0 до {MISSION_GRAM_MAX:g}")
    if not (math.isfinite(meat) and 0 <= meat <= MISSION_MEAT_MAX):
        raise ValueError(f"{n}: Meat — от 0 до {MISSION_MEAT_MAX:,.0f}".replace(",", " "))
    if gram <= 0 and meat <= 0:
        raise ValueError(f"{n}: укажите награду (GRAM или Meat)")
    chat = str(raw.get("chat") or "").strip()
    url = str(raw.get("url") or "").strip()
    if kind == "channel":
        if chat.startswith("https://t.me/") and "+" not in chat:
            chat = "@" + chat[len("https://t.me/"):].strip("/").split("/")[0]
        if not MISSION_CHAT_RE.match(chat):
            raise ValueError(f"{n}: канал для проверки — @username канала или его числовой ID (-100…)")
        if not url and chat.startswith("@"):
            url = f"https://t.me/{chat[1:]}"
        if not url:
            raise ValueError(f"{n}: у приватного канала укажите ссылку-приглашение")
    else:
        chat = ""
        if not url:
            raise ValueError(f"{n}: укажите ссылку")
    if not re.match(r"^https?://\S{3,500}$", url):
        raise ValueError(f"{n}: ссылка должна начинаться с https://")
    mid = str(raw.get("id") or "").strip()
    if not MISSION_ID_RE.match(mid):
        mid = "m" + secrets.token_hex(4)
    out = {"id": mid, "title": title, "type": kind, "gram": gram, "mnstr": meat, "url": url,
           "active": raw.get("active", True) is not False}
    if chat:
        out["chat"] = chat
    return out


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
    await refresh_missions()
    if not missions_on():
        raise HTTPException(status_code=404, detail="Задания временно отключены")

    user_id = authenticate(x_telegram_init_data, request.user_id)
    mission = mission_by_id(request.mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Задание не найдено")

    row = await fetch_user(user_id)
    if request.mission_id in (row.get("missions") or []):
        raise HTTPException(status_code=409, detail="Награда уже получена")

    if mission.get("type") == "channel":
        if not await channel_subscribed(user_id, mission.get("chat") or ""):
            raise HTTPException(status_code=400, detail="Подписка на канал не найдена")

    granted = await store.claim_mission(
        user_id, request.mission_id, float(mission.get("gram") or 0), float(mission.get("mnstr") or 0)
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


def merchant_eagle_buyback() -> dict:
    """{tier: {"price": GRAM, "limit": штук}} из game_config.json -> merchant.eagle_buyback."""
    raw = (CONFIG.get("merchant") or {}).get("eagle_buyback") or {}
    out = {}
    for tier, offer in raw.items():
        try:
            price, limit = float(offer.get("price")), int(offer.get("limit"))
        except (TypeError, ValueError, AttributeError):
            continue
        if tier in TIER_INDEX and price > 0 and limit > 0:
            out[tier] = {"price": price, "limit": limit}
    return out


@app.post("/api/merchant/buy_meat")
async def merchant_buy_meat(request: MerchantBuyMeat, x_telegram_init_data: Optional[str] = Header(None)):
    """Обмен золота на Meat по фиксированному курсу — постоянный, без лимита
    количества: сколько угодно, пока хватает золота (проверяется и
    списывается атомарно, см. store.buy_merchant_meat)."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    await ensure_user(user_id)

    rate = float((CONFIG.get("merchant") or {}).get("meat_per_gold", 5)) or 5.0
    amount = float(request.amount)
    if not math.isfinite(amount) or amount <= 0:
        raise HTTPException(status_code=400, detail="Укажи количество Meat")

    result = await store.buy_merchant_meat(user_id, amount, rate)
    if result["status"] == "insufficient_gold":
        raise HTTPException(status_code=400, detail="Недостаточно золота")
    await record_economy(gold_to_meat=result["cost"], meat_from_gold=result["amount"])

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
    """Выкуп полностью откормленного орла купцом — цена и общий на всех
    игроков лимит зависят от редкости (merchant.eagle_buyback). Лимит и
    ферма продавца проверяются/меняются только на сервере."""
    user_id = authenticate(x_telegram_init_data, request.user_id)
    await ensure_user(user_id)

    result = await store.sell_merchant_eagle(
        user_id, request.slot_index, merchant_eagle_buyback(), MONSTER_TIER, FEED_LEVELS,
    )
    if result["status"] == "limit_reached":
        raise HTTPException(status_code=409, detail="Купец больше не выкупает орлов этой редкости — лимит исчерпан")
    if result["status"] == "not_found":
        raise HTTPException(status_code=400, detail="Орёл не найден")
    if result["status"] == "wrong_tier":
        raise HTTPException(status_code=400, detail="Купец не выкупает орлов этой редкости")
    if result["status"] == "listed":
        raise HTTPException(status_code=400, detail="Орёл выставлен на рынок — сначала сними лот")
    if result["status"] == "on_expedition":
        raise HTTPException(status_code=400, detail="Орёл в экспедиции — купцу его не продать")
    if result["status"] == "not_fed":
        raise HTTPException(status_code=400, detail="Купец берёт только полностью откормленных орлов")
    if result["status"] == "last_eagle":
        raise HTTPException(status_code=400, detail="Нельзя остаться без орлов")
    if result["status"] == "conflict":
        raise HTTPException(status_code=409, detail="Ферма изменилась — попробуй ещё раз")

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "price": result["price"], "tier": result["tier"],
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


MARKET_MAX_PRICE_GRAM = 1_000_000.0


def check_market_price(price: float):
    """Цена лота: конечное число больше нуля и не выше MARKET_MAX_PRICE_GRAM.
    Лот с ценой Infinity не сериализуется в JSON — список лотов падал с 500
    у всех игроков, поэтому NaN/Infinity отсекаем ещё при выставлении."""
    if not math.isfinite(price) or price <= 0:
        raise HTTPException(status_code=400, detail="Цена должна быть больше нуля")
    if price > MARKET_MAX_PRICE_GRAM:
        raise HTTPException(status_code=400, detail=f"Максимальная цена — {MARKET_MAX_PRICE_GRAM:,.0f} GRAM".replace(",", " "))


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
    check_market_price(request.price_gram)
    min_price = _market_min_price(request.monster_id)
    if request.price_gram < min_price:
        raise HTTPException(
            status_code=400,
            detail=f"Минимальная цена для этой редкости — {min_price:g} GRAM",
        )

    row = await fetch_user(user_id)
    listing_id, reason = await store.create_listing(
        user_id, row.get("name") or "", request.monster_id,
        FEED_LEVELS, request.price_gram, int(time.time()), request.slot_index,
    )
    if listing_id is None:
        messages = {
            "listed": "Этот орёл уже выставлен на рынок",
            "on_expedition": "Орёл в экспедиции — его нельзя выставить на рынок",
            "conflict": "Ферма изменилась — попробуй ещё раз",
        }
        raise HTTPException(status_code=400, detail=messages.get(reason, "Нет такого прокачанного орла на ферме"))

    fresh = await store.get(user_id)
    return {"status": "success", "listing_id": listing_id, "monsters": read_farm(fresh["monsters"])}


@app.post("/api/market/buy")
async def market_buy(request: MarketBuyRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Покупает лот — сервер атомарно переводит GRAM и передаёт орла."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    result = await store.buy_listing(
        user_id, request.listing_id, FEED_LEVELS, MARKET_COMMISSION, FARM_QUEUE_MAX,
    )
    if result != "ok":
        messages = {
            "not_found": "Лот уже продан или снят с продажи",
            "own_listing": "Нельзя купить свой же лот",
            "insufficient_funds": "Не хватает GRAM",
            "no_room": "Нет места ни в ячейках, ни в неактивных — освободи место",
            "conflict": "Ферма изменилась — попробуй ещё раз",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось купить"))

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "coins": float(fresh.get("coins") or 0.0),
        "monsters": read_farm(fresh["monsters"]),
        "farm_queue": read_farm(fresh.get("farm_queue"))[:FARM_QUEUE_MAX],
        "slots": int(fresh.get("slots") or START_SLOTS),
        "ops": int(fresh.get("ops") or 0),
    }


@app.post("/api/market/cancel")
async def market_cancel(request: MarketCancelRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Снимает свой лот с продажи — орёл возвращается на ферму."""
    user_id = authenticate(x_telegram_init_data, request.user_id)

    result = await store.cancel_listing(user_id, request.listing_id, FEED_LEVELS, FARM_QUEUE_MAX)
    if result != "ok":
        messages = {
            "not_found": "Лот уже продан или снят с продажи",
            "not_owner": "Это не твой лот",
            "no_room": "Нет места ни в ячейках, ни в неактивных — освободи место",
        }
        raise HTTPException(status_code=400, detail=messages.get(result, "Не удалось снять лот"))

    fresh = await store.get(user_id)
    return {
        "status": "success",
        "monsters": read_farm(fresh["monsters"]),
        "farm_queue": read_farm(fresh.get("farm_queue"))[:FARM_QUEUE_MAX],
        "slots": int(fresh.get("slots") or START_SLOTS),
        "ops": int(fresh.get("ops") or 0),
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
    check_market_price(request.price_gram)
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


def resource_market_min_lot_price(resource: str, amount: int) -> float:
    return max(RESOURCE_MARKET_MIN_PRICE.get(resource, 0.0),
               RESOURCE_MARKET_MIN_UNIT_PRICE.get(resource, 0.0) * amount)


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
    check_market_price(request.price_gram)
    min_price = resource_market_min_lot_price(request.resource, amount)
    if request.price_gram < min_price - 1e-9:
        unit = RESOURCE_MARKET_MIN_UNIT_PRICE.get(request.resource, 0.0)
        per_unit = f" ({unit:g} GRAM за штуку × {amount})" if unit * amount >= min_price else ""
        raise HTTPException(status_code=400, detail=f"Минимальная цена лота — {min_price:g} GRAM{per_unit}")

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
    if not math.isfinite(amount) or amount < MIN_WITHDRAW:
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
async def admin_login(body: AdminLogin, request: Request, response: Response):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD не задан на сервере")
    now = time.time()
    key = admin_client_key(request)
    if admin_login_blocked(key, now):
        raise HTTPException(status_code=429, detail="Слишком много неверных попыток — подождите 15 минут")
    if not secrets.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        ADMIN_LOGIN_FAILS.setdefault(key, []).append(now)
        print(f"[admin] неверный пароль с {key} ({len(ADMIN_LOGIN_FAILS[key])} за 15 мин)")
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
async def admin_me(request: Request, _: None = Depends(require_admin)):
    uid = verified_telegram_id(request)
    return {"status": "success", "telegram_id": uid, "owner": uid in MAINTENANCE_WHITELIST if uid else False}


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


def ton_tx_url(tx_hash: str) -> str:
    """Ссылка на транзакцию в tonviewer. toncenter v3 отдаёт хэш в base64 —
    tonviewer ждёт hex."""
    h = (tx_hash or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", h):
        try:
            raw = base64.b64decode(h.replace("-", "+").replace("_", "/") + "=" * (-len(h) % 4))
            h = raw.hex() if len(raw) == 32 else ""
        except Exception:
            h = ""
    return f"https://tonviewer.com/transaction/{h.lower()}" if h else ""


DEPOSIT_LATE_SECONDS = 3600   # зачислено позже перевода больше чем на час — подозрительно


@app.get("/admin/api/transactions")
async def admin_transactions(kind: str = "all", user_id: str = "", limit: int = 50, offset: int = 0,
                             _: None = Depends(require_admin)):
    """Админка → Транзакции: история пополнений и выводов. Для пополнений —
    дата самого перевода в TON и когда сервер его зачислил; если между ними
    больше часа, строка помечается (так выглядел повтор старых переводов
    после очистки базы)."""
    try:
        kind = kind if kind in ("all", "deposit", "withdrawal") else "all"
        uid = None
        if user_id.strip():
            if not user_id.strip().lstrip("-").isdigit():
                raise HTTPException(status_code=400, detail="ID игрока — число")
            uid = int(user_id.strip())
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        data = await store.list_transactions(kind, uid, limit, offset)
        names = {}
        for it in data["items"]:
            for key in ("user_id", "referrer_id"):
                pid = it.get(key)
                if pid is not None and pid not in names:
                    row = await store.get(int(pid))
                    names[pid] = (row or {}).get("name") or ""
        for it in data["items"]:
            it["name"] = names.get(it["user_id"], "")
            if it["kind"] == "deposit":
                it["ton"] = float(it.get("ton_sent") or 0) if it.get("below_min") else (
                    round(it["amount"] / TON_RATE, 9) if TON_RATE else it["amount"])
                it["tx_url"] = ton_tx_url(it["id"])
                late = (it["credited_at"] - it["ts"]) if it.get("credited_at") else None
                it["late_seconds"] = late
                it["suspicious"] = bool(late is not None and late > DEPOSIT_LATE_SECONDS and not it.get("below_min"))
                if it.get("referrer_id") is not None:
                    it["referrer_name"] = names.get(it["referrer_id"], "")
        data["deposits_since"] = float(await store.get_setting(DEPOSITS_SINCE_SETTING, 0) or 0)
        return data
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось загрузить транзакции: {type(e).__name__}: {e}")


class AdminDepositReverse(BaseModel):
    tx_hash: str


@app.post("/admin/api/deposits/reverse")
async def admin_reverse_deposit(body: AdminDepositReverse, _: None = Depends(require_admin)):
    """Отменить ошибочное зачисление пополнения: списать GRAM у игрока (и
    реферальный бонус у пригласившего, если он записан). Один раз на депозит."""
    try:
        r = await store.reverse_deposit(body.tx_hash.strip(), time.time())
        if r["status"] == "not_found":
            raise HTTPException(status_code=404, detail="Пополнение не найдено")
        if r["status"] == "already_reversed":
            raise HTTPException(status_code=409, detail="Это пополнение уже отменено")
        if r["status"] == "below_min":
            raise HTTPException(status_code=409, detail="Перевод меньше минимума — он и не зачислялся")
        print(f"[admin] отмена пополнения {body.tx_hash}: игрок {r['user_id']} −{r['amount']:g} GRAM"
              + (f", реферер {r['referrer_id']} −{r['referral_gram']:g}" if r.get("referrer_id") else ""))
        return r
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось отменить пополнение: {type(e).__name__}: {e}")


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
    # Техработы через сырой конфиг не переключить — только владельцам
    # через /admin/api/features: берём текущее состояние, а не присланное.
    cfg["maintenance"] = dict(cfg.get("maintenance") or {})
    cfg["maintenance"]["enabled"] = maintenance_on()

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    apply_config(cfg)
    return {"status": "success"}


@app.get("/admin/api/economy")
async def admin_economy(_: None = Depends(require_admin)):
    """Потоки золота/мяса/GRAM за всё время и по дням (последние 14 суток UTC)."""
    return await store.get_economy(14)


@app.post("/admin/api/merchant/reset")
async def admin_reset_merchant(_: None = Depends(require_admin)):
    return await store.reset_merchant_state()


@app.post("/admin/api/features")
async def admin_set_features(body: AdminFeatureToggle, request: Request, _: None = Depends(require_admin)):
    """Включает/выключает колесо фортуны, задания и режим техработ без правки сырого конфига.
    Смена режима техработ — только владельцам (require_owner)."""
    await refresh_maintenance(force=True)
    if bool(body.maintenance_enabled) != maintenance_on():
        require_owner(request)
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
    try:
        state = await save_maintenance_state(enabled=body.maintenance_enabled)
        await save_missions_state(enabled=body.missions_enabled)
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить режим техработ: {type(e).__name__}: {e}")
    print(f"[admin] техработы: {'ВКЛ' if state['maintenance_enabled'] else 'выкл'}, тестеры: {state['testers']}")
    return {
        "wheel_enabled": WHEEL_ENABLED,
        "missions_enabled": missions_on(),
        "maintenance_enabled": state["maintenance_enabled"],
        "testers": state["testers"],
    }


@app.get("/admin/api/features")
async def admin_get_features(_: None = Depends(require_admin)):
    try:
        await refresh_maintenance(force=True)
        await refresh_missions(force=True)
        return {
            "wheel_enabled": WHEEL_ENABLED,
            "missions_enabled": missions_on(),
            "maintenance_enabled": maintenance_on(),
            "testers": sorted(MAINTENANCE_TESTERS),
        }
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось загрузить настройки: {type(e).__name__}: {e}")


class AdminMaintenanceTesters(BaseModel):
    testers: List[int] = []


@app.post("/admin/api/maintenance/testers")
async def admin_set_maintenance_testers(body: AdminMaintenanceTesters, _: int = Depends(require_owner)):
    """Telegram ID игроков, которые заходят в игру во время техработ."""
    try:
        if len(body.testers) > MAINTENANCE_TESTERS_MAX:
            raise HTTPException(status_code=400, detail=f"Не больше {MAINTENANCE_TESTERS_MAX} тестеров")
        state = await save_maintenance_state(testers=body.testers)
        print(f"[admin] тестеры техработ: {state['testers']}")
        return state
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить тестеров: {type(e).__name__}: {e}")


class AdminMissionsSave(BaseModel):
    missions: List[dict]


class AdminMissionsToggle(BaseModel):
    enabled: bool


class AdminMissionChannelCheck(BaseModel):
    chat: str


@app.get("/admin/api/missions")
async def admin_missions(_: None = Depends(require_admin)):
    """Админка → Задания: все задания (и скрытые) + сколько игроков выполнило каждое."""
    try:
        await refresh_missions(force=True)
        items = missions_list(include_hidden=True)
        claims = {}
        for m in items:
            claims[m["id"]] = await store.users.count_documents({"missions": m["id"]})
        return {"enabled": missions_on(), "missions": items, "claims": claims,
                "from_db": _missions_db is not None, "max": MISSIONS_MAX,
                "gram_max": MISSION_GRAM_MAX, "meat_max": MISSION_MEAT_MAX, "bot_username": BOT_USERNAME}
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось загрузить задания: {type(e).__name__}: {e}")


@app.post("/admin/api/missions")
async def admin_save_missions(body: AdminMissionsSave, _: None = Depends(require_admin)):
    """Сохраняет весь список заданий (порядок = порядок в игре). id выполненных
    заданий не меняются, поэтому правка названия/награды не даёт забрать
    награду второй раз; удалённое задание просто исчезает из игры."""
    try:
        if len(body.missions) > MISSIONS_MAX:
            raise HTTPException(status_code=400, detail=f"Не больше {MISSIONS_MAX} заданий")
        out, seen = [], set()
        for i, raw in enumerate(body.missions):
            try:
                m = normalize_mission(raw, i)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if m["id"] in seen:
                m["id"] = "m" + secrets.token_hex(4)
            seen.add(m["id"])
            out.append(m)
        await save_missions_state(missions=out)
        print(f"[admin] задания сохранены: {len(out)} шт. ({sum(1 for m in out if m['active'])} активных)")
        return await admin_missions()
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить задания: {type(e).__name__}: {e}")


@app.post("/admin/api/missions/enabled")
async def admin_toggle_missions(body: AdminMissionsToggle, _: None = Depends(require_admin)):
    try:
        await save_missions_state(enabled=body.enabled)
        return {"enabled": missions_on()}
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось переключить задания: {type(e).__name__}: {e}")


@app.post("/admin/api/missions/check_channel")
async def admin_check_mission_channel(body: AdminMissionChannelCheck, _: None = Depends(require_admin)):
    """Проверка для задания «подписка»: видит ли бот канал и админ ли он там —
    без прав админа Telegram не скажет, подписан ли игрок, и задание никому
    не засчитается."""
    chat = (body.chat or "").strip()
    if chat.startswith("https://t.me/") and "+" not in chat:
        chat = "@" + chat[len("https://t.me/"):].strip("/").split("/")[0]
    if not MISSION_CHAT_RE.match(chat):
        raise HTTPException(status_code=400, detail="Укажите @username канала или его числовой ID (-100…)")
    if not BOT_TOKEN:
        return {"ok": False, "chat": chat, "detail": "BOT_TOKEN не задан на сервере — проверить подписку нельзя"}
    try:
        from telegram import Bot
        from telegram.error import TelegramError
        bot = Bot(BOT_TOKEN)
        try:
            info = await bot.get_chat(chat)
        except TelegramError as e:
            return {"ok": False, "chat": chat, "detail": f"Бот не видит канал {chat}: {e}. Добавьте бота в канал администратором."}
        me = await bot.get_me()
        try:
            member = await bot.get_chat_member(chat, me.id)
            status = member.status
        except TelegramError as e:
            status = f"ошибка: {e}"
        ok = status in ("administrator", "creator")
        title = getattr(info, "title", "") or chat
        return {"ok": ok, "chat": chat, "title": title, "bot_status": status,
                "detail": f"«{title}»: бот — администратор, подписка проверяется" if ok
                else f"«{title}»: бот не администратор ({status}). Сделайте @{me.username} админом канала, иначе задание не засчитается."}
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось проверить канал: {type(e).__name__}: {e}")


# --- АДМИН: УПРАВЛЕНИЕ КЛАНАМИ ---

@app.get("/admin/api/clans")
async def admin_list_clans(search: str = "", limit: int = 50, offset: int = 0,
                            _: None = Depends(require_admin)):
    clans, total = await store.list_clans_admin(search, limit, offset)
    items = [{
        "id": c["id"], "name": c.get("name") or "", "leader_id": c.get("leader_id"),
        "member_count": len(c.get("members") or []), "member_limit": CLAN_MEMBER_LIMIT,
        "open_slots": int(c.get("open_slots") or 0), "clan_power": float(c.get("clan_power") or 0),
        "applications_count": len(c.get("applications") or []), "created_at": c.get("created_at"),
    } for c in clans]
    await refresh_clans_enabled(force=True)
    return {"items": items, "total": total, "clans_enabled": _clans_enabled}


class AdminClansToggle(BaseModel):
    enabled: bool


@app.post("/admin/api/clans/enabled")
async def admin_toggle_clans(body: AdminClansToggle, _: None = Depends(require_admin)):
    """Включить/выключить раздел «Кланы» в игре (кнопка во вкладке «Кланы» админки)."""
    try:
        await store.set_setting(CLANS_SETTING, bool(body.enabled))
        await refresh_clans_enabled(force=True)
        print(f"[admin] раздел кланов: {'ВКЛ' if _clans_enabled else 'выкл'}")
        return {"clans_enabled": _clans_enabled}
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось переключить кланы: {type(e).__name__}: {e}")


# --- АДМИН: ТЕСТОВЫЕ КЛАН-БОТЫ ---
# ВАЖНО: объявлены ДО /admin/api/clans/{clan_id} — иначе FastAPI сопоставит
# GET /admin/api/clans/bots с шаблоном {clan_id} и вернёт «Клан не найден».
# Только для ручного тестирования турнирного пайплайна самим админом — эти
# кланы полностью реальны для сервера (участвуют в /api/clan/top и в
# автоматическом турнире Топ-32 наравне со всеми), но помечены is_bot=True
# и полностью изолированы от реальных игроков (свои фейковые user_id).
# Удаляются одним действием через /admin/api/clans/bots/clear.
#
# Каждый клан-бот сразу «одет» для настоящего боя 10х10 в «Прямом Эфире»:
# CLAN_ROSTER_SIZE ботов-участников, у каждого на ферме один орёл
# максимального уровня (FEED_LEVELS) с полным комплектом снаряжения на эту
# редкость, и утверждённая расстановка (approved_lineup) — ровно то, что
# сервер проверяет в момент матча (_clan_roster_fighters). Статы бойцов
# (hp/atk/def/crit/spd) нигде не хранятся — их, как и у живых игроков,
# считает combat_eagle_stats из редкости и снаряжения.

# clan_power бота → базовая редкость его бойцов (индекс в CONFIG["tiers"]) и
# грейд снаряжения: <300 обычная/серое … ≥4000 мифическая/мифическое. Двое
# первых бойцов на редкость выше, трое последних — ниже, чтобы бой был
# разнообразным, а сильный по clan_power бот был сильнее и в бою.
BOT_POWER_TIER_STEPS = (300, 800, 1500, 2500, 4000)


def bot_clan_roster_size() -> int:
    return max(1, min(CLAN_ROSTER_SIZE, CLAN_MEMBER_LIMIT))


def bot_clan_loadouts(clan_power: float) -> list:
    """Снаряжение бойцов клан-бота — список по одному на бойца:
    {tier_id, farm_slot (орёл макс. уровня в формате read_farm), equipped
    ({claws/armor/mask/ring: грейд})}."""
    tiers = list(TIER_INDEX)
    base = sum(1 for step in BOT_POWER_TIER_STEPS if clan_power >= step)
    size = bot_clan_roster_size()
    loadouts = []
    for i in range(size):
        offset = 1 if i < 2 else (-1 if i >= size - 3 else 0)
        tier_idx = min(max(base + offset, 0), len(tiers) - 1)
        tier_id = tiers[tier_idx]
        grade = NEST_GRADES[min(tier_idx, len(NEST_GRADES) - 1)]
        loadouts.append({
            "tier_id": tier_id,
            "farm_slot": {"id": tier_id, "next_egg_at": 0, "feed_level": FEED_LEVELS,
                          "feed_taps": 0, "expedition_until": 0},
            "equipped": {item_type: grade for item_type in NEST_TYPE_ORDER},
        })
    return loadouts


async def create_equipped_bot_clan(name: str, clan_power: float, member_count: int) -> str:
    """Клан-бот, сразу готовый к бою: не меньше bot_clan_roster_size()
    участников, орлы, снаряжение и утверждённая расстановка."""
    return await store.create_bot_clan(
        name, clan_power, max(member_count, bot_clan_roster_size()), bot_clan_loadouts(clan_power),
    )


async def migrate_bot_clan_rosters() -> int:
    """Одноразовая по сути миграция (запускается при старте сервера, см.
    startup_event, и кнопкой в админке): находит клан-ботов, созданных до
    появления снаряжения, у которых в момент матча набралось бы меньше
    bot_clan_roster_size() бойцов, и «одевает» их. Уже одетых не трогает,
    так что повторные перезапуски ничего не меняют. Возвращает, сколько
    кланов одето."""
    fixed, _errors = await _migrate_bot_clan_rosters_detailed()
    return fixed


async def _migrate_bot_clan_rosters_detailed() -> tuple:
    """То же, что migrate_bot_clan_rosters, но кланы обрабатываются по
    одному: ошибка на одном клане (например, повреждённый документ) не
    мешает одеть остальные и возвращается списком [(имя, текст ошибки)]."""
    fixed, errors = 0, []
    for clan in await store.list_bot_clans():
        try:
            if len(await _clan_roster_fighters(clan)) >= bot_clan_roster_size():
                continue
            if await store.equip_bot_clan(clan["id"], bot_clan_loadouts(float(clan.get("clan_power") or 0))):
                fixed += 1
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)
            errors.append((clan.get("name") or clan.get("id"), f"{type(e).__name__}: {e}"[:200]))
    return fixed, errors


@app.post("/admin/api/clans/bots")
async def admin_create_bot_clan(body: AdminCreateBotClan, _: None = Depends(require_admin)):
    """Один тестовый клан-бот с заданной силой — для проверки конкретного
    сценария (например, клан ровно на нужном месте посева)."""
    name = (body.name or "").strip()[:24] or "Bot Clan"
    power = max(0.0, float(body.clan_power))
    count = max(1, min(int(body.member_count), CLAN_MEMBER_LIMIT))
    clan_id = await create_equipped_bot_clan(name, power, count)
    clan = await store.get_clan(clan_id)
    return {"status": "success", "clan_id": clan_id, "clan": clan_view(clan)}


@app.post("/admin/api/clans/bots/bulk")
async def admin_bulk_create_bot_clans(body: AdminBulkCreateBotClans, _: None = Depends(require_admin)):
    """Массово создаёт count тестовых клан-ботов со случайной clan_power в
    [min_power, max_power] — быстрый способ набрать полное поле (напр. 31
    клан) для обкатки турнира Топ-32/16/8, не дожидаясь регистрации
    реальных кланов."""
    count = max(1, min(int(body.count), 32))
    member_count = max(1, min(int(body.member_count), CLAN_MEMBER_LIMIT))
    lo, hi = float(body.min_power), float(body.max_power)
    if lo > hi:
        lo, hi = hi, lo
    created = 0
    for _i in range(count):
        power = random.uniform(lo, hi)
        await create_equipped_bot_clan(f"Bot Clan {random.randint(1000, 9999)}", power, member_count)
        created += 1
    return {"status": "success", "created": created}


@app.get("/admin/api/clans/bots")
async def admin_list_bot_clans(_: None = Depends(require_admin)):
    clans = await store.list_bot_clans()
    items = [{
        "id": c["id"], "name": c.get("name") or "", "member_count": len(c.get("members") or []),
        "clan_power": float(c.get("clan_power") or 0),
    } for c in clans]
    return {"items": items, "total": len(items)}


@app.post("/admin/api/clans/bots/equip")
async def admin_equip_bot_clans(_: None = Depends(require_admin)):
    """То же, что миграция при старте сервера — одевает клан-ботов без
    готовой расстановки, не дожидаясь перезапуска."""
    fixed, errors = await _migrate_bot_clan_rosters_detailed()
    return {"status": "success", "equipped": fixed, "errors": [{"clan": n, "error": e} for n, e in errors]}


@app.post("/admin/api/clans/bots/clear")
async def admin_clear_bot_clans(_: None = Depends(require_admin)):
    """Удаляет ВСЕ тестовые клан-боты одним действием — откат после
    тестирования, реальных игроков не касается."""
    try:
        removed = await store.clear_bot_clans()
        print(f"[admin] удалены боты: кланов {removed['clans']}, ботов-игроков {removed['users']}")
        return {"status": "success", "removed": removed["clans"], "removed_users": removed["users"]}
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось удалить ботов: {type(e).__name__}: {e}")


@app.get("/admin/api/clans/{clan_id}")
async def admin_get_clan(clan_id: str, _: None = Depends(require_admin)):
    """Полная карточка клана для админ-панели: состав с именами и личным
    burned_power каждого, заявки на вступление (с самым сильным орлом
    заявителя), расстановка — то же самое, что видит лидер клана внутри
    игры, плюс created_at для сортировки."""
    clan = await store.get_clan(clan_id)
    if not clan:
        raise HTTPException(status_code=404, detail="Клан не найден")

    member_names, member_power = {}, {}
    for uid in clan.get("members") or []:
        row = await store.get(uid)
        if row:
            member_names[str(uid)] = row.get("name") or ""
            member_power[str(uid)] = float(row.get("burned_power") or 0)

    applicants = []
    for uid in clan.get("applications") or []:
        row = await store.get(uid)
        if row:
            applicants.append({"user_id": uid, "name": row.get("name") or "", "strongest_eagle": strongest_eagle_info(row)})

    return {
        "id": clan["id"], "name": clan.get("name") or "", "leader_id": clan.get("leader_id"),
        "members": clan.get("members") or [], "member_names": member_names, "member_power": member_power,
        "member_limit": CLAN_MEMBER_LIMIT, "open_slots": int(clan.get("open_slots") or 0),
        "clan_power": float(clan.get("clan_power") or 0), "applications": applicants,
        "approved_lineup": clan.get("approved_lineup") or [], "lineup_submissions": clan.get("lineup_submissions") or {},
        "created_at": clan.get("created_at"),
    }


@app.post("/admin/api/clans/{clan_id}/rename")
async def admin_rename_clan(clan_id: str, body: AdminClanRename, _: None = Depends(require_admin)):
    name = (body.name or "").strip()[:24]
    if not name:
        raise HTTPException(status_code=400, detail="Название не может быть пустым")
    if not await store.admin_rename_clan(clan_id, name):
        raise HTTPException(status_code=404, detail="Клан не найден")
    return {"status": "success", "name": name}


@app.post("/admin/api/clans/{clan_id}/set_slots")
async def admin_set_clan_slots(clan_id: str, body: AdminClanSetSlots, _: None = Depends(require_admin)):
    slots = max(0, min(CLAN_MEMBER_LIMIT, int(body.open_slots)))
    if not await store.admin_set_clan_open_slots(clan_id, slots):
        raise HTTPException(status_code=404, detail="Клан не найден")
    return {"status": "success", "open_slots": slots}


@app.post("/admin/api/clans/{clan_id}/kick")
async def admin_kick_clan_member(clan_id: str, body: AdminClanKick, _: None = Depends(require_admin)):
    """Админ принудительно исключает ЛЮБОГО участника (включая лидера —
    лидерство при этом переходит следующему по списку, см. leave_clan)."""
    result = await store.leave_clan(body.user_id, clan_id)
    if result != "ok":
        raise HTTPException(status_code=400, detail="Не удалось исключить участника (уже не в этом клане?)")
    return {"status": "success"}


@app.post("/admin/api/clans/{clan_id}/disband")
async def admin_force_disband_clan(clan_id: str, _: None = Depends(require_admin)):
    """Админ распускает ЛЮБОЙ клан без проверки лидерства — см.
    store.admin_disband_clan (в отличие от disband_clan, которым может
    воспользоваться только сам лидер)."""
    if not await store.admin_disband_clan(clan_id):
        raise HTTPException(status_code=404, detail="Клан не найден")
    return {"status": "success"}


# --- АДМИН: ТУРНИР КЛАНОВ (ручной запуск) ---

@app.get("/admin/api/clan_tournament")
async def admin_clan_tournament_status(_: None = Depends(require_admin)):
    """Текущее состояние турнира кланов для админ-панели: сколько кланов
    сейчас на сервере (включая созданных админом тестовых клан-ботов),
    допустимые масштабы турнира и, если турнир уже запущен, его сетка и
    статус."""
    clan_count = await store.count_clans()
    tournament = await current_clan_tournament()
    view = None
    if tournament:
        finished = bool(tournament["bracket"]) and all(m.get("resolved") for m in tournament["bracket"])
        view = {
            "cycle": tournament["cycle"], "size": tournament["size"],
            "start_at": tournament["start_at"], "finished": finished,
            "match_minute": int(tournament.get("match_minute", CLAN_MATCH_MINUTE_DEFAULT)),
            "bracket": clan_tournament_view_bracket(tournament),
        }
    return {
        "clan_count": clan_count, "sizes": list(CLAN_TOURNAMENT_SIZES),
        "default_match_minute": CLAN_MATCH_MINUTE_DEFAULT, "tournament": view,
    }


@app.post("/admin/api/clan_tournament/start")
async def admin_start_clan_tournament(body: AdminClanTournamentStart, _: None = Depends(require_admin)):
    """«[Админ] Утвердить состав и Запустить Турнир»: берёт ровно первые
    size сильнейших кланов сервера по clan_power (сам турнир ботов не
    генерирует — только те кланы, что уже есть, в т.ч. созданные админом
    тестовые), строит сетку и замораживает состав. Если кланов меньше
    выбранного масштаба — отказывает, не запуская турнир."""
    if body.size not in CLAN_TOURNAMENT_SIZES:
        raise HTTPException(status_code=400, detail="Недопустимый масштаб турнира")
    match_minute = CLAN_MATCH_MINUTE_DEFAULT if body.match_minute is None else body.match_minute
    if not 0 <= match_minute < 1440:
        raise HTTPException(status_code=400, detail="Недопустимое время матчей")

    clan_count = await store.count_clans()
    if clan_count < body.size:
        raise HTTPException(
            status_code=400,
            detail=f"Недостаточно кланов для этого режима: нужно {body.size}, сейчас зарегистрировано {clan_count}",
        )

    top_clans = await store.list_top_clans(body.size)
    bracket = build_clan_bracket(top_clans, body.size)
    start_at = (int(time.time()) // 86400) * 86400  # полночь UTC текущих суток — день 1 турнира
    result = await store.try_launch_clan_tournament(bracket, body.size, start_at, match_minute)
    if result != "ok":
        raise HTTPException(status_code=400, detail="Турнир уже идёт — дождитесь его завершения или отмените его")
    return {"status": "success"}


@app.post("/admin/api/clan_tournament/reconcile")
async def admin_clan_tournament_reconcile(_: None = Depends(require_admin)):
    """Немедленно разрешает назревшие матчи — те же, что разрешились бы
    при следующем обращении игрока к /api/clan/tournament."""
    await reconcile_clan_tournament()
    return await admin_clan_tournament_status()


@app.post("/admin/api/clan_tournament/match_time")
async def admin_set_clan_tournament_match_time(body: AdminClanTournamentMatchTime,
                                               _: None = Depends(require_admin)):
    """Меняет время дня (UTC), в которое открываются матчи турнира, — для
    всех ещё не сыгранных матчей без ручного переноса. Если новое время уже
    прошло, назревшие матчи разыгрываются сразу."""
    if not 0 <= body.match_minute < 1440:
        raise HTTPException(status_code=400, detail="Недопустимое время матчей")
    if not await store.set_clan_tournament_match_minute(body.match_minute):
        raise HTTPException(status_code=404, detail="Турнир не запущен")
    await reconcile_clan_tournament()
    return await admin_clan_tournament_status()


@app.post("/admin/api/clan_tournament/match/{match_index}/time")
async def admin_set_clan_match_time(match_index: int, body: AdminClanMatchTime,
                                    _: None = Depends(require_admin)):
    """Переносит один ещё не сыгранный матч на другое время (now — открыть
    прямо сейчас) или сбрасывает перенос (start_time=None) — матч снова
    идёт по расписанию турнира. Матч всё равно ждёт, пока сыграны матчи
    предыдущего раунда, которые заполняют его слоты."""
    start_time = time.time() if body.now else body.start_time
    if start_time is not None and start_time <= 0:
        raise HTTPException(status_code=400, detail="Недопустимое время матча")
    if not await store.set_clan_match_start_override(match_index, start_time):
        raise HTTPException(status_code=404, detail="Матч не найден или уже сыгран")
    await reconcile_clan_tournament()
    return await admin_clan_tournament_status()


@app.post("/admin/api/clan_tournament/cancel")
async def admin_cancel_clan_tournament(_: None = Depends(require_admin)):
    """Отменяет текущий турнир целиком (сетка удаляется, результаты не
    сохраняются) — чтобы перезапустить его с другим составом/масштабом,
    не дожидаясь финала. Кланы и игроки не затрагиваются."""
    await store.cancel_clan_tournament()
    return {"status": "success"}


# --- NFT «НЕБЕСНЫЙ ОРЕЛ»: еженедельный сбор Небесного Осколка ---
# Изолированный модуль: только чтение кошелька через TON API, без минтинга и
# вывода NFT. Порядок проверок в /api/nft/claim-shard: кошелёк привязан ->
# на нём есть NFT из MY_OFFICIAL_NFT_COLLECTION -> прошёл интервал (задаётся
# в админке, по умолчанию 7 дней) -> начисление. Два таймера:
#   * игрока (last_shard_claim) — не чаще раза за интервал на аккаунт;
#   * самой NFT (коллекция nft_claims) — одна NFT даёт не больше одного
#     осколка за интервал, даже если её адрес привязали несколько аккаунтов
#     (адрес кошелька в игре сохраняется без доказательства владения) или
#     NFT передали другу после сбора.

class NftApiError(Exception):
    """TON API недоступен или ответил ошибкой — это не «NFT нет»."""


_NFT_CHECK_CACHE: dict = {}   # wallet -> (checked_at, [nft raw addresses])


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def ton_address_to_raw(address: str) -> str:
    """Нормализация адреса TON (аналог Address.parse(addr) из @ton/core):
    принимает дружественный вид в любом варианте — bounceable EQ…,
    non-bounceable UQ…, тестнет-флаг (kQ…/0Q…), base64url или обычный
    base64 — и сырой "wc:hex" в любом регистре. Возвращает единый вид
    "wc:hex" (hex в нижнем регистре), по которому адреса можно честно
    сравнивать. Контрольная сумма CRC16 проверяется: опечатка -> ValueError."""
    address = (address or "").strip()
    m = re.fullmatch(r"(-?\d+):([0-9a-fA-F]{64})", address)
    if m:
        return f"{int(m.group(1))}:{m.group(2).lower()}"
    import base64
    if len(address) != 48:
        raise ValueError(f"bad TON address length: {address!r}")
    std = address.replace("-", "+").replace("_", "/")
    try:
        data = base64.b64decode(std, validate=True)
    except Exception:
        raise ValueError(f"bad TON address encoding: {address!r}")
    if len(data) != 36:
        raise ValueError(f"bad TON address size: {address!r}")
    if _crc16_xmodem(data[:34]) != int.from_bytes(data[34:36], "big"):
        raise ValueError(f"bad TON address checksum: {address!r}")
    if (data[0] & 0x3F) not in (0x11, 0x51):   # 0x11 bounceable, 0x51 non-bounceable, +0x80 testnet
        raise ValueError(f"bad TON address flags: {address!r}")
    wc = data[1] if data[1] < 128 else data[1] - 256
    return f"{wc}:{data[2:34].hex()}"


def ton_address_to_friendly(raw: str, bounceable: bool = True) -> str:
    """Сырой адрес -> дружественный mainnet (EQ… или UQ…) — для логов и диагностики."""
    import base64
    wc, h = ton_address_to_raw(raw).split(":")
    body = bytes([0x11 if bounceable else 0x51, int(wc) & 0xFF]) + bytes.fromhex(h)
    return base64.urlsafe_b64encode(body + _crc16_xmodem(body).to_bytes(2, "big")).decode()


def ton_bounceable(address: str) -> str:
    """Единый стандарт для запросов к TON Center: Bounceable mainnet (EQ…).
    Кошелёк из TonConnect приходит как UQ… (non-bounceable), может прийти и
    сырым 0:… — всё приводится к EQ…, как
    Address(addr).to_string(is_user_friendly=True, is_url_safe=True, is_bounceable=True)."""
    address = (address or "").strip()
    raw = ton_address_to_raw(address)   # проверка формата и CRC16; ValueError при опечатке
    if TonAddress is not None:
        try:
            return TonAddress(raw).to_string(is_user_friendly=True, is_url_safe=True,
                                             is_bounceable=True, is_test_only=False)
        except Exception as e:
            print(f"[nft] tonsdk не разобрал адрес {address!r}: {e} — используем встроенную нормализацию")
    return ton_address_to_friendly(raw, bounceable=True)


# Действующая коллекция «Небесного орла». По умолчанию — константа выше;
# админ может сменить её в админке (Обзор → NFT) — адрес хранится в БД
# (settings: nft_collection) и переживает перезапуск/деплой. Все проверки
# читают эти три переменные; их обновляет apply_nft_collection.
OFFICIAL_COLLECTION = MY_OFFICIAL_NFT_COLLECTION
OFFICIAL_COLLECTION_RAW = ton_address_to_raw(MY_OFFICIAL_NFT_COLLECTION)
OFFICIAL_COLLECTION_EQ = ton_bounceable(MY_OFFICIAL_NFT_COLLECTION)
NFT_COLLECTION_SETTING = "nft_collection"
NFT_COLLECTION_REFRESH_SECONDS = 30   # другие копии сервера подхватят смену за полминуты
_nft_collection_loaded_at = 0.0


def apply_nft_collection(address: str) -> bool:
    """Делает address действующей коллекцией. True — если она сменилась
    (тогда кеш проверок кошельков сбрасывается: он считан по старой)."""
    global OFFICIAL_COLLECTION, OFFICIAL_COLLECTION_RAW, OFFICIAL_COLLECTION_EQ
    raw = ton_address_to_raw(address)
    changed = raw != OFFICIAL_COLLECTION_RAW
    OFFICIAL_COLLECTION_RAW = raw
    OFFICIAL_COLLECTION_EQ = ton_bounceable(raw)
    OFFICIAL_COLLECTION = OFFICIAL_COLLECTION_EQ
    if changed:
        _NFT_CHECK_CACHE.clear()
        print(f"[nft] официальная коллекция: {OFFICIAL_COLLECTION_EQ} ({OFFICIAL_COLLECTION_RAW})")
    return changed


async def refresh_nft_collection(force: bool = False) -> None:
    """Подтягивает адрес коллекции из БД (не чаще раза в 30 с). Сбой базы или
    битый адрес в ней — остаёмся на текущей коллекции."""
    global _nft_collection_loaded_at
    now = time.time()
    if not force and now - _nft_collection_loaded_at < NFT_COLLECTION_REFRESH_SECONDS:
        return
    _nft_collection_loaded_at = now
    try:
        saved = await store.get_setting(NFT_COLLECTION_SETTING, "")
        apply_nft_collection(saved or MY_OFFICIAL_NFT_COLLECTION)
    except Exception as e:
        print(f"[nft] не удалось прочитать коллекцию из БД: {type(e).__name__}: {e}")


def _mainnet_url(url: str, default: str) -> str:
    if "testnet" in (url or "").lower():
        print(f"[nft] {url} — это TESTNET, коллекция существует только в mainnet; используем {default}")
        return default
    return url or default


def _nft_matches(nft_raw: str, collection_raw: str) -> bool:
    """NFT относится к «Небесному орлу»: её коллекция — официальная (обычный
    случай) или адрес в константе оказался адресом самой NFT-карточки."""
    return OFFICIAL_COLLECTION_RAW in (collection_raw, nft_raw)


def _safe_raw(address) -> str:
    try:
        return ton_address_to_raw(address) if address else ""
    except ValueError:
        return ""


async def _nfts_from_tonapi(client, wallet_raw: str) -> dict:
    """Все NFT кошелька через tonapi.io (mainnet), включая выставленные на
    продажу (indirect_ownership) — без серверного фильтра по коллекции, чтобы
    сверять адрес коллекции самим, после нормализации."""
    url = f"{_mainnet_url(TONAPI_URL, TONAPI_MAINNET)}/v2/accounts/{wallet_raw}/nfts"
    headers = {"Accept": "application/json"}
    if TONAPI_KEY:
        headers["Authorization"] = f"Bearer {TONAPI_KEY}"
    final_url = str(httpx.URL(url, params={"limit": 1000, "offset": 0, "indirect_ownership": "true"}))
    print(f"Выполняю запрос к TON: {final_url}")
    resp = await client.get(final_url, headers=headers)
    result = {"provider": "tonapi", "status": resp.status_code, "url": final_url, "items": [], "body": resp.text[:2000]}
    if resp.status_code == 200:
        for item in (resp.json() or {}).get("nft_items") or []:
            result["items"].append({
                "address": _safe_raw(item.get("address")),
                "collection": _safe_raw((item.get("collection") or {}).get("address")),
                "collection_name": (item.get("collection") or {}).get("name") or "",
            })
    return result


async def _toncenter_nft_query(client, params: dict) -> dict:
    """Один GET к toncenter.com /api/v3/nft/items c заголовком X-API-Key.
    В лог — точный URL (ключ только в заголовке, в URL его нет) и ответ
    индексера, чтобы при пустом списке было видно, что вернул блокчейн."""
    base = f"{_mainnet_url(TONCENTER_URL, TONCENTER_MAINNET)}/api/v3/nft/items"
    final_url = str(httpx.URL(base, params=params))
    headers = {"Accept": "application/json", "X-API-Key": TONCENTER_API_KEY}
    print(f"Выполняю запрос к TON: {final_url}")
    try:
        response = await client.get(base, params=params, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:2000]}
        print(f"[TON TEST] Запрос к API: {params}, Ответ сервера: {json.dumps(payload, ensure_ascii=False)[:6000]}")
    except Exception as e:
        print(f"[TON TEST] Запрос к API: {params}, Ошибка: {type(e).__name__}: {e}")
        raise
    result = {"status": response.status_code, "url": final_url, "items": [], "body": response.text[:2000]}
    if response.status_code == 200 and isinstance(payload, dict):
        for item in payload.get("nft_items") or []:
            result["items"].append({
                "address": _safe_raw(item.get("address")),
                "collection": _safe_raw(item.get("collection_address")),
                "collection_name": "",
            })
    return result


async def _nfts_from_toncenter(client, wallet_raw: str) -> dict:
    """toncenter.com API v3 (mainnet): NFT коллекции «Небесный орел» у игрока.
    Кошелёк игрока и адрес коллекции перед запросом приводятся к единому
    виду Bounceable EQ… (ton_bounceable), запрос —
    GET /api/v3/nft/items?owner_address=<EQ…>&collection_address=<EQ…>.
    Если пусто — ещё две попытки: с include_on_sale=true (NFT выставлена на
    продажу и формально лежит на контракте продажи) и без фильтра по
    коллекции (видно, какие NFT вообще есть на кошельке; находится и NFT,
    если в константе оказался адрес самой карточки)."""
    params = {
        "owner_address": ton_bounceable(wallet_raw),
        "collection_address": OFFICIAL_COLLECTION_EQ,
    }
    result = dict(await _toncenter_nft_query(client, params), provider="toncenter")
    if result["status"] != 200 or result["items"]:
        return result
    for extra in ({**params, "include_on_sale": "true"},
                  {"owner_address": params["owner_address"], "include_on_sale": "true", "limit": 1000}):
        retry = await _toncenter_nft_query(client, extra)
        if retry["status"] == 200 and retry["items"]:
            return dict(retry, provider="toncenter", url=f"{result['url']} (пусто) → {retry['url']}")
    return result


async def check_wallet_nfts(wallet: str) -> dict:
    """Полная проверка кошелька (для /api/nft/* и админской диагностики):
    {"wallet_raw", "found": [адреса NFT коллекции], "providers": [...], "ok": хоть
    один провайдер ответил}. Сначала toncenter.com (с ключом TONCENTER_API_KEY);
    если он ошибся или не нашёл нашу NFT — перепроверяем через tonapi.io."""
    try:
        wallet_raw = ton_address_to_raw(wallet)
    except ValueError as e:
        print(f"[nft] invalid wallet address saved for player: {e}")
        return {"wallet_raw": "", "found": [], "providers": [], "ok": True, "invalid_wallet": True}

    await refresh_nft_collection()
    report = {"wallet_raw": wallet_raw, "found": [], "providers": [], "ok": False}
    async with httpx.AsyncClient(timeout=12) as client:
        providers = (_nfts_from_toncenter, _nfts_from_tonapi) if TONCENTER_API_KEY else (_nfts_from_tonapi, _nfts_from_toncenter)
        for fetch in providers:
            try:
                res = await fetch(client, wallet_raw)
            except httpx.HTTPError as e:
                res = {"provider": fetch.__name__.replace("_nfts_from_", ""), "status": 0,
                       "items": [], "body": f"{type(e).__name__}: {e}"}
            report["providers"].append(res)
            if res["status"] == 200:
                report["ok"] = True
                matched = [it["address"] for it in res["items"] if _nft_matches(it["address"], it["collection"])]
                for nft in matched:
                    if nft not in report["found"]:
                        report["found"].append(nft)
            if res["status"] != 200 or not report["found"]:
                # Подробный лог (аналог console.error): что именно ответил провайдер.
                seen = sorted({f"{it['collection']} {it['collection_name']}".strip() for it in res["items"]})[:20]
                print(f"[nft] {res['provider']} status={res['status']} wallet={wallet_raw} "
                      f"items={len(res['items'])} matched=0 official={OFFICIAL_COLLECTION_RAW} "
                      f"collections_seen={seen} body={res['body'][:1500]!r}")
            if report["found"]:
                break
    return report


async def fetch_collection_nfts(wallet: str) -> List[str]:
    """Адреса NFT «Небесного орла» на кошельке wallet. Кеш
    NFT_CHECK_CACHE_SECONDS на кошелёк (только удачные ответы). Если ни один
    провайдер не ответил — NftApiError (это «не удалось проверить», а не
    «NFT нет»)."""
    await refresh_nft_collection()
    now = time.time()
    cached = _NFT_CHECK_CACHE.get(wallet)
    if cached and now - cached[0] < NFT_CHECK_CACHE_SECONDS:
        return cached[1]
    report = await check_wallet_nfts(wallet)
    if not report["ok"]:
        codes = ", ".join(f"{p['provider']}={p['status']}" for p in report["providers"])
        raise NftApiError(f"all TON providers failed ({codes})")
    _NFT_CHECK_CACHE[wallet] = (now, report["found"])
    return report["found"]


async def nft_claim_interval() -> int:
    value = await store.get_setting("nft_claim_interval_seconds", NFT_CLAIM_INTERVAL_DEFAULT)
    try:
        return max(NFT_CLAIM_INTERVAL_MIN, min(NFT_CLAIM_INTERVAL_MAX, int(value)))
    except (TypeError, ValueError):
        return NFT_CLAIM_INTERVAL_DEFAULT


def fmt_wait(seconds: float) -> str:
    """Оставшееся время по-русски: «2 д 5 ч 13 мин» / «4 мин 10 с»."""
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d} д")
    if h: parts.append(f"{h} ч")
    if m: parts.append(f"{m} мин")
    if not d and not h: parts.append(f"{s} с")
    return " ".join(parts)


def nft_player_view(row: dict, interval: int, now: float) -> dict:
    last = float(row.get("last_shard_claim") or 0)
    next_at = last + interval if last else 0.0
    return {
        "sky_shards": int(row.get("sky_shards") or 0),
        "last_shard_claim": last,
        "interval_seconds": interval,
        "next_claim_at": next_at,
        "can_claim": now >= next_at,
        "server_time": now,
    }


@app.get("/api/nft/status")
async def nft_status(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Состояние блока «Моя NFT коллекция» в профиле: привязан ли кошелёк,
    есть ли на нём NFT «Небесный орел», счётчик собранных осколков и время
    следующего сбора."""
    try:
        user_id = authenticate(x_telegram_init_data, user_id)
        row = await fetch_user(user_id)
        now = time.time()
        view = nft_player_view(row, await nft_claim_interval(), now)
        wallet = (row.get("wallet") or "").strip()
        view.update({"wallet_connected": bool(wallet), "has_nft": False, "nft_count": 0, "check_failed": False})
        if wallet:
            try:
                nfts = await fetch_collection_nfts(wallet)
                view.update({"has_nft": bool(nfts), "nft_count": len(nfts)})
            except NftApiError as e:
                print(f"[nft] status check failed for {user_id}: {e}")
                view["check_failed"] = True
        return view
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail="Не удалось получить статус NFT")


@app.post("/api/nft/claim-shard")
async def nft_claim_shard(request: NftClaimRequest, x_telegram_init_data: Optional[str] = Header(None)):
    """Еженедельный сбор: +1 Небесный Осколок (в Кузницу — он сразу начинает
    давать частички) и +1 к счётчику sky_shards. Все проверки на сервере."""
    try:
        user_id = authenticate(x_telegram_init_data, request.user_id)
        row = await fetch_user(user_id)
        wallet = (row.get("wallet") or "").strip()
        if not wallet:
            raise HTTPException(status_code=400, detail="Кошелек не подключен")

        try:
            nfts = await fetch_collection_nfts(wallet)
        except NftApiError as e:
            print(f"[nft] claim check failed for {user_id}: {e}")
            raise HTTPException(status_code=503, detail="Не удалось проверить NFT в блокчейне — попробуйте через минуту")
        if not nfts:
            raise HTTPException(status_code=404, detail="NFT 'Небесный орел' не найдена")

        interval = await nft_claim_interval()
        now = time.time()
        last = float(row.get("last_shard_claim") or 0)
        if last and now - last < interval:
            raise HTTPException(status_code=429, detail=f"Следующий сбор через {fmt_wait(last + interval - now)}")

        # Одна NFT — не больше одного осколка за интервал на все аккаунты.
        reserved = None
        for nft in nfts:
            prev = await store.reserve_nft_claim(nft, user_id, now, interval)
            if prev is not False:
                reserved = (nft, prev)
                break
        if reserved is None:
            wait = await store.nft_claim_wait(nfts, now, interval)
            raise HTTPException(
                status_code=429,
                detail=f"Эта NFT уже принесла осколок за текущий период — следующий через {fmt_wait(wait)}",
            )

        def compute(fresh):
            last_claim = float(fresh.get("last_shard_claim") or 0)
            if last_claim and now - last_claim < interval:
                raise HTTPException(status_code=429, detail=f"Следующий сбор через {fmt_wait(last_claim + interval - now)}")
            miners = normalize_nest_miners(fresh.get("nest_miners"))
            new_last_claim = nest_settle_particles(fresh, now, len(miners) + 1)
            miners.append({"id": f"nft-{user_id}-{int(now * 1000)}"})
            sky = int(fresh.get("sky_shards") or 0) + 1
            fields = {"sky_shards": sky, "last_shard_claim": now,
                      "nest_miners": miners, "nest_last_claim": new_last_claim}
            return fields, {"sky_shards": sky, "shard_count": len(miners), "last_claim": new_last_claim}

        try:
            extra = await run_farm_action(user_id, compute)
        except Exception:
            await store.release_nft_claim(reserved[0], reserved[1])   # игроку не начислили — NFT свободна
            raise
        view = nft_player_view({"sky_shards": extra["sky_shards"], "last_shard_claim": now}, interval, now)
        view.update({"success": True, "shard_count": extra["shard_count"], "nest_last_claim": extra["last_claim"], "ops": extra["ops"],
                     "wallet_connected": True, "has_nft": True, "nft_count": len(nfts)})
        return view
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail="Не удалось забрать осколок — попробуйте ещё раз")


@app.get("/admin/api/nft/settings")
async def admin_nft_settings(_: None = Depends(require_admin)):
    await refresh_nft_collection(force=True)
    return {
        "interval_seconds": await nft_claim_interval(),
        "default_seconds": NFT_CLAIM_INTERVAL_DEFAULT,
        "min_seconds": NFT_CLAIM_INTERVAL_MIN,
        "collection": OFFICIAL_COLLECTION,
        "collection_raw": OFFICIAL_COLLECTION_RAW,
        "collection_default": MY_OFFICIAL_NFT_COLLECTION,
        "is_production_mode": IS_PRODUCTION_MODE,
    }


@app.get("/admin/api/nft/check")
async def admin_nft_check(wallet: str, _: None = Depends(require_admin)):
    """Диагностика: что видят tonapi.io и toncenter.com на кошельке wallet
    (mainnet) — статус ответа, сколько NFT, какие коллекции, найдена ли наша."""
    report = await check_wallet_nfts(wallet)
    providers = []
    for p in report["providers"]:
        collections = {}
        for it in p["items"]:
            key = it["collection"] or "(без коллекции)"
            collections.setdefault(key, {"name": it["collection_name"], "count": 0})["count"] += 1
        providers.append({
            "provider": p["provider"], "status": p["status"], "items": len(p["items"]), "url": p.get("url", ""),
            "collections": collections, "error": p["body"][:500] if p["status"] != 200 else "",
        })
    return {
        "wallet_input": wallet, "wallet_raw": report["wallet_raw"],
        "wallet_friendly": ton_address_to_friendly(report["wallet_raw"], bounceable=False) if report["wallet_raw"] else "",
        "invalid_wallet": bool(report.get("invalid_wallet")),
        "official_collection": OFFICIAL_COLLECTION, "official_collection_raw": OFFICIAL_COLLECTION_RAW,
        "found": report["found"], "providers": providers, "ok": report["ok"],
    }


@app.post("/admin/api/nft/collection")
async def admin_set_nft_collection(body: AdminNftCollection, _: None = Depends(require_admin)):
    """Сменить коллекцию «Небесного орла». Адрес — в любом виде (EQ…/UQ…/0:…),
    сохраняется как EQ…; пустая строка — вернуть коллекцию по умолчанию."""
    try:
        address = (body.address or "").strip() or MY_OFFICIAL_NFT_COLLECTION
        try:
            eq = ton_bounceable(address)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректный адрес коллекции (проверьте, что скопирован целиком)")
        await store.set_setting(NFT_COLLECTION_SETTING, eq)
        apply_nft_collection(eq)
        print(f"[admin] коллекция NFT изменена на {eq}")
        return await admin_nft_settings(None)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить коллекцию: {type(e).__name__}: {e}")


@app.post("/admin/api/nft/settings")
async def admin_set_nft_settings(body: AdminNftSettings, _: int = Depends(require_owner)):
    """Интервал сбора осколка по NFT (сек). Хранится в БД (settings), поэтому
    переживает перезапуск и деплой. 604800 = 7 дней — боевой режим.
    Только владельцам (require_owner)."""
    value = int(body.interval_seconds)
    if not NFT_CLAIM_INTERVAL_MIN <= value <= NFT_CLAIM_INTERVAL_MAX:
        raise HTTPException(status_code=400, detail="Интервал должен быть от 1 минуты до 365 дней")
    await store.set_setting("nft_claim_interval_seconds", value)
    return await admin_nft_settings(None)


# --- ЖУРНАЛ БАЛАНСОВ ИГРОКА (админка → Игроки → «История балансов») ---
# Сами записи делает storage.LedgerUsers; здесь — человекочитаемые подписи
# источников и выдача журнала админке. Порядок важен: сначала более точные
# префиксы, потом общие.
LEDGER_LABELS = [
    ("start", "Стартовый баланс"),
    ("vip:meat", "VIP: ежедневное мясо"),
    ("ton:deposit", "Пополнение TON"),
    ("arena:season_rewards", "Награда сезона Арены"),
    ("auction:refund", "Аукцион: возврат ставки"),
    ("auction:reward", "Аукцион: награда"),
    ("/api/auction/bid", "Аукцион: ставка"),
    ("/api/save", "Сохранение клиента"),
    ("/api/farm/feed", "Кормление орла"),
    ("/api/farm/collect_egg", "Сбор яйца"),
    ("/api/farm/fusion_attempt", "Скрещивание"),
    ("/api/farm/expedition/start", "Экспедиция: отправка"),
    ("/api/farm/expedition/collect", "Экспедиция: награда"),
    ("/api/farm/buy_slot", "Покупка слота фермы"),
    ("/api/farm/delete_eagle", "Удаление орла"),
    ("/api/nest/shard/buy", "Покупка Небесного Осколка"),
    ("/api/nest/craft", "Крафт снаряжения"),
    ("/api/nest/upgrade", "Улучшение снаряжения"),
    ("/api/nest/", "Гнездо Воинов"),
    ("/api/arena/buy_energy", "Энергия Арены"),
    ("/api/arena/fight", "Бой на Арене"),
    ("/admin/api/arena/distribute_rewards", "Награды Арены (вручную)"),
    ("/api/clan/create", "Создание клана"),
    ("/api/clan/open_slot", "Место в клане"),
    ("/api/clan/", "Кланы"),
    ("/api/eggs/unlock_slot", "Открытие ячейки яиц"),
    ("/api/eggs/open_all", "Вскрытие всех яиц"),
    ("/api/eggs/open", "Вскрытие яйца"),
    ("/api/eggs/merge", "Слияние яиц"),
    ("/api/vip/buy", "Покупка VIP"),
    ("/api/mission/claim", "Награда за задание"),
    ("/api/daily/claim", "Ежедневный бонус"),
    ("/api/wheel/spin", "Колесо фортуны"),
    ("/api/merchant/buy_meat", "Купец: мясо за золото"),
    ("/api/merchant/sell_eagle", "Купец: продажа орла"),
    ("/api/market/equip/", "Рынок снаряжения"),
    ("/api/market/resources/", "Рынок ресурсов"),
    ("/api/market/", "Рынок орлов"),
    ("/api/ton/withdraw", "Вывод TON"),
    ("/api/ton/check", "Пополнение TON"),
    ("/admin/api/withdrawals", "Вывод: решение админа"),
    ("/admin/api/deposits/reverse", "Отмена пополнения админом"),
    ("/admin/api/players", "Правка админом"),
    ("/api/nft/", "NFT «Небесный орел»"),
]


def ledger_label(source: str) -> str:
    for prefix, label in LEDGER_LABELS:
        if source == prefix or source.startswith(prefix):
            return label
    return source or "—"


@app.get("/admin/api/players/{user_id}/ledger")
async def admin_player_ledger(user_id: int, currency: str = "", limit: int = 100,
                              before: Optional[float] = None, _: None = Depends(require_admin)):
    """История изменений GRAM / Meat / золота игрока: итоги по источникам
    (за всё время журнала) и последние записи (постранично, before = ts)."""
    try:
        limit = max(1, min(500, int(limit)))
        entries = await store.list_ledger(user_id, currency, limit, before)
        summary = await store.ledger_summary(user_id)
        row = await store.get(user_id) or {}
        by_source = sorted(
            ({"source": src, "label": ledger_label(src), **vals} for src, vals in summary["by_source"].items()),
            key=lambda r: -(abs(r["gold"]) + abs(r["mnstr"]) + abs(r["coins"]) * 100),
        )
        return {
            "user_id": user_id,
            "balance": {f: float(row.get(f) or 0) for f in ("coins", "mnstr", "gold")},
            "first_ts": summary["first_ts"],
            "by_source": by_source,
            "entries": [dict(e, label=ledger_label(e["source"])) for e in entries],
            "has_more": len(entries) == limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось загрузить журнал: {type(e).__name__}: {e}")


# --- АУКЦИОН «РАЗДАЧА НЕБЕСНЫХ ОРЛОВ» ---
# Один главный лот за раз, ТОП-5 ставок в реальном времени (клиент опрашивает
# /api/auction каждые пару секунд). Ставка всегда «лидер + 1 Gram» (первая —
# не меньше 10 Gram): игрок встаёт на 1-е место, остальные сдвигаются, 6-й
# выбывает и мгновенно получает замороженные Gram назад. По таймеру ТОП-5
# фиксируется: их ставки списываются навсегда, а в профиль («Мои NFT»)
# добавляется выигранный лот — «Ожидает ручной отправки» (NFT админ
# отправляет сам). Вся денежная логика — в storage (place_auction_bid /
# settle_auction), атомарно и с безопасным повтором.

AUCTION_IMAGE_RE = re.compile(r"https?://[^\s\"'<>]{1,2000}")


def auction_player_name(row: dict) -> str:
    return ((row.get("name") or "").strip() or f"Игрок {row.get('user_id')}")[:32]


def auction_balances(row: dict) -> dict:
    """Балансы, которые меняет аукцион (ставки и награды), — чтобы клиент
    обновил шапку без перезагрузки."""
    return {"gold": float(row.get("gold") or 0), "mnstr": float(row.get("mnstr") or 0),
            "shard_count": len(normalize_nest_miners(row.get("nest_miners"))),
            "nest_last_claim": float(row.get("nest_last_claim") or 0), "ops": int(row.get("ops") or 0)}


def auction_frozen(row: dict) -> float:
    return round(sum(float(v or 0) for v in (row.get("auction_holds") or {}).values()), 6)


def auction_view(doc: Optional[dict], user_id: Optional[int], now: float) -> Optional[dict]:
    if not doc:
        return None
    top = doc.get("top") or []
    leader_bid = float(top[0]["bid"]) if top else 0.0
    next_bid = max(float(doc["min_bid"]), leader_bid + float(doc["step"])) if top else float(doc["min_bid"])
    rows = [{"place": i, "name": e.get("name") or "", "bid": float(e["bid"]),
             "is_me": user_id is not None and int(e["user_id"]) == user_id}
            for i, e in enumerate(top, start=1)]
    mine = next((r for r in rows if r["is_me"]), None)
    return {
        "id": doc["id"], "title": doc.get("title") or AUCTION_DEFAULT_TITLE,
        "item_image": doc.get("item_image") or "", "description": doc.get("description") or "",
        "status": doc["status"], "ends_at": float(doc["ends_at"]), "server_time": now,
        "min_bid": float(doc["min_bid"]), "step": float(doc["step"]), "top_size": AUCTION_TOP_SIZE,
        "top": rows, "next_bid": next_bid, "my_place": mine["place"] if mine else 0,
        "my_bid": mine["bid"] if mine else 0.0, "is_leader": bool(rows and rows[0]["is_me"]),
        "reward_type": doc.get("reward_type") or "nft",
        "reward_amount": float(doc.get("reward_amount") or 1),
        "reward_label": AUCTION_REWARD_TYPES.get(doc.get("reward_type") or "nft", "NFT Карточка"),
        "antisnipe_seconds": AUCTION_ANTISNIPE_SECONDS, "extensions": int(doc.get("extensions") or 0),
    }


class _NothingToCredit(Exception):
    pass


async def credit_auction_rewards(user_id: int) -> list:
    """Начисляет игроку ресурсы за выигранные лоты (статус pending_credit):
    Небесные осколки (настоящие, в Кузницу, + счётчик sky_shards), золото,
    мясо. Одна CAS-запись (run_farm_action) и для ресурсов, и для смены
    статуса на credited — поэтому повтор (фон, /api/auction, сбой посреди)
    никогда не начислит дважды. Возвращает начисленные выигрыши."""
    row = await store.get(user_id)
    if not row or not any(w.get("status") == "pending_credit" for w in row.get("auction_wins") or []):
        return []
    now = time.time()

    def compute(fresh):
        wins = [dict(w) for w in fresh.get("auction_wins") or []]
        pending = [w for w in wins if w.get("status") == "pending_credit"]
        if not pending:
            raise _NothingToCredit()
        total = {"sky_shards": 0, "gold": 0.0, "meat": 0.0}
        for w in pending:
            kind = w.get("reward_type")
            if kind in total:
                total[kind] += int(w.get("reward_amount") or 0) if kind == "sky_shards" else float(w.get("reward_amount") or 0)
            w["status"], w["credited_at"] = "credited", now
        fields = {"auction_wins": wins}
        extra = {"credited": pending}
        if total["sky_shards"]:
            miners = normalize_nest_miners(fresh.get("nest_miners"))
            new_last_claim = nest_settle_particles(fresh, now, len(miners) + total["sky_shards"])
            miners.extend({"id": f"auction-{user_id}-{int(now * 1000)}-{i}"} for i in range(total["sky_shards"]))
            fields.update({"nest_miners": miners, "nest_last_claim": new_last_claim,
                           "sky_shards": int(fresh.get("sky_shards") or 0) + total["sky_shards"]})
            extra.update({"shard_count": len(miners), "nest_last_claim": new_last_claim})
        if total["gold"]:
            fields["gold"] = float(fresh.get("gold") or 0) + total["gold"]
        if total["meat"]:
            fields["mnstr"] = float(fresh.get("mnstr") or 0) + total["meat"]
        return fields, extra

    try:
        with ledger_source("auction:reward"):
            result = await run_farm_action(user_id, compute)
    except _NothingToCredit:
        return []
    for w in result["credited"]:
        print(f"[auction] игроку {user_id} начислено: {w.get('reward_amount'):g} × {w.get('reward_type')} "
              f"(лот {w.get('auction_id')}, {w.get('place')}-е место)")
    return result["credited"]


_auction_reward_sweep_at = 0.0


async def sweep_auction_rewards(now: float) -> None:
    """Подстраховка раз в 5 минут: игроки, у которых остались неначисленные
    награды (например, сервер перезапустился посреди расчёта лота)."""
    global _auction_reward_sweep_at
    if now - _auction_reward_sweep_at < 300:
        return
    _auction_reward_sweep_at = now
    for uid in await store.users_with_pending_auction_rewards():
        try:
            await credit_auction_rewards(uid)
        except Exception as e:
            print(f"[auction] reward credit failed for {uid}: {type(e).__name__}: {e}")


async def auction_tick(now: Optional[float] = None) -> None:
    """Закрыть лоты с истёкшим таймером и довести их расчёт до конца.
    Вызывается фоновым циклом и лениво из /api/auction — повтор безопасен."""
    now = time.time() if now is None else now
    try:
        # Отложенные возвраты выбывшим из ТОП-5 (см. process_auction_refunds) —
        # добиваем и без новых ставок.
        for auction_id in await store.auctions_with_pending_refunds():
            await store.process_auction_refunds(auction_id)
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
    for doc in await store.list_due_auctions(now):
        try:
            if doc["status"] == "active":
                if await store.finish_auction(doc["id"], now):
                    print(f"[auction] {doc['id']} закрыт по таймеру, ТОП-{len(doc.get('top') or [])}")
                continue   # расчёт — после короткой паузы (AUCTION_SETTLE_GRACE)
            if now - float(doc.get("finished_at") or doc["ends_at"]) < AUCTION_SETTLE_GRACE:
                continue
            result = await store.settle_auction(doc["id"])
            if result:
                await record_economy(auction_gram=result["spent"])
                for w in result["winners"]:
                    try:
                        await credit_auction_rewards(int(w["user_id"]))
                    except Exception as e:
                        print(f"[auction] reward credit failed for {w['user_id']}: {type(e).__name__}: {e}")
                names = ", ".join(f"{w['place']}. {w['name']} ({w['bid']:g})" for w in result["winners"]) or "ставок не было"
                print(f"[auction] {doc['id']} рассчитан ({doc['status']}): {names}")
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)


async def ledger_maintenance():
    """Раз в 6 часов чистит журнал балансов от записей старше 90 дней."""
    while True:
        try:
            removed = await store.prune_ledger()
            if removed:
                print(f"[ledger] удалено старых записей: {removed}")
        except Exception as e:
            print(f"[ledger] prune failed: {type(e).__name__}: {e}")
        await asyncio.sleep(6 * 3600)


async def auction_worker():
    while True:
        try:
            await auction_tick()
            await sweep_auction_rewards(time.time())
        except Exception as e:
            print(f"[auction] worker error: {type(e).__name__}: {e}")
        await asyncio.sleep(AUCTION_WORKER_SECONDS)


async def auction_for_player() -> Optional[dict]:
    """Идущий лот, а если его нет — последний завершённый (чтобы игроки видели
    итоговый ТОП-5 и победителей)."""
    return await store.get_active_auction() or await store.get_latest_auction()


@app.get("/api/auction")
async def auction_state(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    try:
        user_id = authenticate(x_telegram_init_data, user_id)
        now = time.time()
        await auction_tick(now)
        await credit_auction_rewards(user_id)
        row = await fetch_user(user_id)
        return {
            "auction": auction_view(await auction_for_player(), user_id, now),
            "server_time": now, "coins": float(row.get("coins") or 0), "frozen": auction_frozen(row),
            **auction_balances(row),
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail="Не удалось загрузить аукцион")


@app.post("/api/auction/bid")
async def auction_bid(request: AuctionBidRequest, x_telegram_init_data: Optional[str] = Header(None)):
    try:
        user_id = authenticate(x_telegram_init_data, request.user_id)
        # Цену ставки считает ТОЛЬКО сервер: ставка лидера + AUCTION_BID_STEP
        # (или минимальная). amount — лишь «какую цену видел игрок» для сверки:
        # не совпала с серверной — 409 «ставку перебили», а цену из запроса
        # сервер не использует никогда. Мусор (NaN, ±Infinity, ≤0) — сразу 400.
        if request.amount is not None and not (math.isfinite(request.amount) and request.amount > 0):
            raise HTTPException(status_code=400, detail="Некорректная сумма ставки")
        row = await fetch_user(user_id)
        now = time.time()
        result = await store.place_auction_bid(
            request.auction_id, user_id, auction_player_name(row), request.amount, now, AUCTION_TOP_SIZE,
            antisnipe_seconds=AUCTION_ANTISNIPE_SECONDS,
        )
        status = result["status"]
        if status == "not_found":
            raise HTTPException(status_code=404, detail="Лот не найден")
        if status == "ended":
            raise HTTPException(status_code=400, detail="Аукцион уже завершён")
        if status == "already_leader":
            raise HTTPException(status_code=409, detail="Вы и так на 1-м месте — ждите, пока вас перебьют")
        if status == "price_changed":
            raise HTTPException(status_code=409, detail=f"Ставку перебили — теперь нужно {result['required']:g} Gram")
        if status == "insufficient":
            raise HTTPException(status_code=400, detail=f"Недостаточно Gram: для ставки {result['required']:g} нужно {result['delta']:g} свободных Gram")
        if status != "ok":
            raise HTTPException(status_code=409, detail="Много ставок одновременно — попробуйте ещё раз")
        fresh = await fetch_user(user_id)
        return {
            "success": True, "bid": result["bid"], "charged": result["delta"],
            "extended": bool(result.get("extended_to")), "extended_to": result.get("extended_to"),
            "auction": auction_view(await store.get_auction(request.auction_id), user_id, now),
            "server_time": now, "coins": float(fresh.get("coins") or 0), "frozen": auction_frozen(fresh),
            "ops": int(fresh.get("ops") or 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail="Не удалось сделать ставку — попробуйте ещё раз")


@app.get("/api/auction/wins")
async def auction_wins(user_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    """Вкладка «Мои NFT»: выигранные на аукционе лоты игрока."""
    try:
        user_id = authenticate(x_telegram_init_data, user_id)
        await credit_auction_rewards(user_id)
        row = await fetch_user(user_id)
        wins = sorted(row.get("auction_wins") or [], key=lambda w: -float(w.get("won_at") or 0))
        return {"wins": [{
            "auction_id": w.get("auction_id"), "title": w.get("title") or AUCTION_DEFAULT_TITLE,
            "item_image": w.get("item_image") or "", "bid": float(w.get("bid") or 0),
            "place": int(w.get("place") or 0), "won_at": float(w.get("won_at") or 0),
            "status": w.get("status") or "pending_delivery",
            "reward_type": w.get("reward_type") or "nft", "reward_amount": float(w.get("reward_amount") or 1),
            "reward_label": AUCTION_REWARD_TYPES.get(w.get("reward_type") or "nft", "NFT Карточка"),
        } for w in wins], **auction_balances(row)}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail="Не удалось загрузить выигранные лоты")


async def admin_auction_payload() -> dict:
    now = time.time()
    history = []
    for doc in await store.list_auctions(10):
        winners = []
        for w in doc.get("winners") or []:
            user = await store.get(int(w["user_id"])) or {}
            winners.append(dict(w, wallet=user.get("wallet") or ""))
        history.append({
            "id": doc["id"], "title": doc.get("title") or "", "item_image": doc.get("item_image") or "",
            "status": doc["status"], "settled": bool(doc.get("settled")), "created_at": doc.get("created_at"),
            "ends_at": doc["ends_at"], "min_bid": doc["min_bid"], "top": doc.get("top") or [], "winners": winners,
            "reward_type": doc.get("reward_type") or "nft", "reward_amount": float(doc.get("reward_amount") or 1),
            "reward_label": AUCTION_REWARD_TYPES.get(doc.get("reward_type") or "nft", "NFT Карточка"),
        })
    active = await store.get_active_auction()
    return {
        "active": auction_view(active, None, now), "history": history, "server_time": now,
        "is_production_mode": IS_PRODUCTION_MODE, "default_title": AUCTION_DEFAULT_TITLE,
        "min_bid": AUCTION_MIN_BID, "step": AUCTION_BID_STEP, "top_size": AUCTION_TOP_SIZE,
        "reward_types": AUCTION_REWARD_TYPES, "antisnipe_seconds": AUCTION_ANTISNIPE_SECONDS,
    }


@app.get("/admin/api/auction")
async def admin_auction(_: None = Depends(require_admin)):
    try:
        await auction_tick()
        return await admin_auction_payload()
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось загрузить аукцион: {type(e).__name__}: {e}")


@app.post("/admin/api/auction/create")
async def admin_auction_create(body: AdminAuctionCreate, _: None = Depends(require_admin)):
    try:
        title = (body.title or "").strip()[:80] or AUCTION_DEFAULT_TITLE
        image = (body.item_image or "").strip()
        if image and not AUCTION_IMAGE_RE.fullmatch(image):
            raise HTTPException(status_code=400, detail="Ссылка на картинку должна начинаться с https:// (без пробелов и кавычек)")
        duration = int(body.duration_seconds)
        if not AUCTION_DURATION_MIN <= duration <= AUCTION_DURATION_MAX:
            raise HTTPException(status_code=400, detail="Длительность — от 1 минуты до 30 дней")
        min_bid = float(body.min_bid)
        if not AUCTION_MIN_BID <= min_bid <= 1_000_000:
            raise HTTPException(status_code=400, detail=f"Стартовая ставка — не меньше {AUCTION_MIN_BID:g} Gram")
        reward_type = (body.reward_type or "nft").strip()
        if reward_type not in AUCTION_REWARD_TYPES:
            raise HTTPException(status_code=400, detail="Неизвестный тип награды")
        try:
            reward_amount = float(body.reward_amount)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Укажите количество награды числом")
        if reward_type in ("nft", "sky_shards"):
            if reward_amount != int(reward_amount):
                raise HTTPException(status_code=400, detail="Количество NFT и осколков — целое число")
            reward_amount = int(reward_amount)
        if not 0 < reward_amount <= AUCTION_REWARD_MAX[reward_type]:
            raise HTTPException(status_code=400, detail=f"Количество — от 1 до {AUCTION_REWARD_MAX[reward_type]:g} на каждого победителя")
        await auction_tick()
        now = time.time()
        doc = await store.create_auction(title, image, (body.description or "").strip()[:300],
                                         min_bid, AUCTION_BID_STEP, now, now + duration,
                                         reward_type=reward_type, reward_amount=reward_amount)
        if not doc:
            raise HTTPException(status_code=409, detail="Уже идёт другой лот (или предыдущий ещё рассчитывается) — дождитесь конца или отмените его")
        print(f"[auction] создан лот {doc['id']} «{title}» на {duration} с, старт {min_bid:g} Gram, "
              f"награда каждому: {reward_amount:g} × {reward_type}")
        return await admin_auction_payload()
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось создать лот: {type(e).__name__}: {e}")


@app.post("/admin/api/auction/finish")
async def admin_auction_finish(body: AdminAuctionAction, _: None = Depends(require_admin)):
    """Досрочно завершить: текущий ТОП-5 становится победителями."""
    try:
        if not await store.finish_auction(body.auction_id, time.time(), force=True):
            raise HTTPException(status_code=409, detail="Лот уже не активен")
        return await admin_auction_payload()
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось завершить лот: {type(e).__name__}: {e}")


@app.post("/admin/api/auction/cancel")
async def admin_auction_cancel(body: AdminAuctionAction, _: None = Depends(require_admin)):
    """Отменить лот без победителей: все замороженные ставки возвращаются."""
    try:
        if not await store.cancel_auction(body.auction_id, time.time()):
            raise HTTPException(status_code=409, detail="Лот уже не активен")
        return await admin_auction_payload()
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось отменить лот: {type(e).__name__}: {e}")


@app.post("/admin/api/auction/delivery")
async def admin_auction_delivery(body: AdminAuctionDelivery, _: None = Depends(require_admin)):
    try:
        if not await store.set_auction_delivery(body.auction_id, body.user_id, body.delivered):
            raise HTTPException(status_code=404, detail="Победитель не найден")
        return await admin_auction_payload()
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить отметку: {type(e).__name__}: {e}")


@app.post("/admin/api/test/auction-timer")
async def admin_test_auction_timer(_: None = Depends(require_admin)):
    """[Админ-Тест] Таймер идущего лота = 05:30 — чтобы проверить антиснайпер:
    дождаться < 05:00, сделать ставку и увидеть, как таймер прыгнул на 05:00.
    Только пока IS_PRODUCTION_MODE выключен."""
    try:
        if IS_PRODUCTION_MODE:
            raise HTTPException(status_code=403, detail="Недоступно: сервер в боевом режиме (IS_PRODUCTION_MODE)")
        active = await store.get_active_auction()
        if not active:
            raise HTTPException(status_code=404, detail="Нет идущего лота — сначала создайте лот")
        await store.set_auction_ends(active["id"], time.time() + AUCTION_TEST_TIMER_SECONDS)
        print(f"[admin-test] таймер лота {active['id']} установлен на 05:30")
        return await admin_auction_payload()
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось установить таймер: {type(e).__name__}: {e}")


@app.post("/admin/api/test/shorten-timers")
async def admin_test_shorten_timers(_: int = Depends(require_owner)):
    """[Админ-Тест] Все таймеры — до 1 минуты: идущий лот заканчивается через
    минуту (если ему оставалось больше), интервал сбора осколка по NFT —
    1 минута. Только пока IS_PRODUCTION_MODE выключен."""
    try:
        if IS_PRODUCTION_MODE:
            raise HTTPException(status_code=403, detail="Недоступно: сервер в боевом режиме (IS_PRODUCTION_MODE)")
        now = time.time()
        active = await store.get_active_auction()
        shortened = bool(active) and await store.shorten_auction(active["id"], now + 60)
        await store.set_setting("nft_claim_interval_seconds", 60)
        print(f"[admin-test] таймеры сокращены: аукцион={'да' if shortened else 'нет'}, NFT-интервал=60 с")
        payload = await admin_auction_payload()
        payload.update({"auction_shortened": shortened, "nft_interval_seconds": 60})
        return payload
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise HTTPException(status_code=500, detail=f"Не удалось сократить таймеры: {type(e).__name__}: {e}")


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


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin — кнопка, открывающая админку внутри Telegram: тогда её запросы
    подписаны initData, и владельцу доступны техработы и интервал NFT.
    Остальным команда ничего не отвечает (не светим, что она есть)."""
    try:
        user = update.effective_user
        if not user or user.id not in MAINTENANCE_WHITELIST or not WEB_APP_URL:
            return
        url = WEB_APP_URL.rstrip("/") + "/admin"
        keyboard = [[InlineKeyboardButton("🛠 Открыть админку", web_app=WebAppInfo(url=url))]]
        await update.message.reply_text(
            "Админка SkyLords GRAMM. Открытая отсюда, она подтверждает ваш Telegram ID — "
            "доступны техработы, тестеры и интервал NFT.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        print(f"[bot] /admin failed: {type(e).__name__}: {e}")


BOT_MENU_BUTTON_TEXT = "🎮 Играть"


async def setup_bot_menu(bot) -> None:
    """Кнопка меню бота (слева от поля ввода) открывает игру сразу, без /start:
    ставится для всех чатов при каждом запуске, поэтому всегда указывает на
    актуальный WEB_APP_URL (https). Заодно — подсказка команды /start."""
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text=BOT_MENU_BUTTON_TEXT, web_app=WebAppInfo(url=WEB_APP_URL))
        )
        print(f"[bot] кнопка меню «{BOT_MENU_BUTTON_TEXT}» → {WEB_APP_URL}")
    except Exception as e:
        print(f"[bot] не удалось поставить кнопку меню: {type(e).__name__}: {e}")
    try:
        await bot.set_my_commands([BotCommand("start", "Открыть игру")])
    except Exception as e:
        print(f"[bot] не удалось задать команды: {type(e).__name__}: {e}")


async def run_bot():
    global BOT_USERNAME

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))

    await application.initialize()
    # Имя из getMe надёжнее ручной переменной: без опечаток и лишней @.
    me = await application.bot.get_me()
    if me.username:
        BOT_USERNAME = me.username
        print(f"Bot username: @{BOT_USERNAME}")
    await setup_bot_menu(application.bot)
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
    try:
        # Бесплатные стартовые места клана (clans.initial_open_slots) выросли —
        # кланы, созданные раньше, получают недостающие места бесплатно.
        raised = await store.raise_clan_open_slots(min(CLAN_INITIAL_OPEN_SLOTS, CLAN_MEMBER_LIMIT))
        if raised:
            print(f"[clans] бесплатные места подняты до {CLAN_INITIAL_OPEN_SLOTS} у кланов: {raised}")
    except Exception as e:
        print(f"[clans] raise_clan_open_slots FAILED: {type(e).__name__}: {e}")
    try:
        equipped = await migrate_bot_clan_rosters()
        if equipped:
            print(f"[bots] одето клан-ботов для боя 10х10: {equipped}")
    except Exception as e:  # тестовые данные не должны мешать запуску игры
        print(f"[bots] migrate_bot_clan_rosters FAILED: {type(e).__name__}: {e}")
    await refresh_nft_collection(force=True)
    await refresh_maintenance(force=True)
    await refresh_missions(force=True)
    await refresh_clans_enabled(force=True)
    asyncio.create_task(auction_worker())
    asyncio.create_task(ledger_maintenance())
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
