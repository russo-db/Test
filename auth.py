"""Проверка подписи Telegram WebApp initData.

Telegram подписывает данные, которые Mini App получает при запуске. Проверив
подпись секретом бота, сервер узнаёт настоящий user_id и перестаёт зависеть от
того, что прислал клиент.
"""

import hashlib
import hmac
import json
import re
import time
from typing import Optional
from urllib.parse import parse_qsl


INIT_DATA_MAX_LEN = 8192          # настоящая initData — пара сотен байт
AUTH_DATE_FUTURE_SKEW = 300       # допуск на расхождение часов, сек
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> Optional[dict]:
    """Разбирает и проверяет initData по официальному алгоритму Telegram:
    secret = HMAC_SHA256(key="WebAppData", msg=bot_token);
    hash   = HMAC_SHA256(key=secret, msg=data_check_string), где
    data_check_string — все поля, кроме hash, в формате key=value,
    отсортированные по ключу и склеенные через \n.

    Возвращает все поля, в которых `user` уже распакован, а `start_param`
    несёт полезную нагрузку реферальной ссылки. Любое расхождение — None:
    без токена бота, слишком длинная строка, повтор ключа, кривой hash,
    неверная подпись, просроченная или «будущая» auth_date, нет user.id.
    initDataUnsafe с клиента не проверяется и серверу не нужен вовсе.
    """
    try:
        if not init_data or not bot_token or len(init_data) > INIT_DATA_MAX_LEN:
            return None

        # keep_blank_values обязателен: пустые значения тоже участвуют в подписи
        pairs_list = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        pairs = dict(pairs_list)
        if len(pairs) != len(pairs_list):
            return None  # повтор ключа — так initData не выглядит никогда
        received = pairs.pop("hash", None)
        if not received or not _HASH_RE.match(received):
            return None

        check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None

        auth_date = int(pairs.get("auth_date", "0"))
        now = time.time()
        if auth_date <= 0 or auth_date > now + AUTH_DATE_FUTURE_SKEW:
            return None
        if max_age and now - auth_date > max_age:
            return None

        user = json.loads(pairs.get("user") or "null")
        if not isinstance(user, dict) or not isinstance(user.get("id"), int) or isinstance(user.get("id"), bool) or user["id"] <= 0:
            return None
    except (ValueError, TypeError, UnicodeError):
        return None

    pairs["user"] = user
    return pairs
