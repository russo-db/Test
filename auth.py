"""Проверка подписи Telegram WebApp initData.

Telegram подписывает данные, которые Mini App получает при запуске. Проверив
подпись секретом бота, сервер узнаёт настоящий user_id и перестаёт зависеть от
того, что прислал клиент.
"""

import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl


def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> Optional[dict]:
    """Разбирает и проверяет initData.

    Возвращает все поля, в которых `user` уже распакован, а `start_param`
    несёт полезную нагрузку реферальной ссылки. Оба значения подписаны
    Telegram, поэтому подменить их нельзя.
    """
    if not init_data or not bot_token:
        return None

    # keep_blank_values обязателен: пустые значения тоже участвуют в подписи
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None

    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age and (auth_date <= 0 or time.time() - auth_date > max_age):
        return None

    try:
        user = json.loads(pairs.get("user") or "null")
    except ValueError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None

    pairs["user"] = user
    return pairs
