import logging

import database

logger = logging.getLogger(__name__)

TABLE = "settings"

DEFAULTS = {
    "temperature": 1.0,
    "top_p": 1.0,
    "max_tokens": 4096,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
}

_KEY = "gen_{}"


def _all() -> dict:
    out = {}
    for name, default in DEFAULTS.items():
        raw = database.get_value(TABLE, _KEY.format(name))
        out[name] = raw if raw is not None else default
    return out


def get(name: str):
    """Вернуть текущее значение параметра (или дефолт)."""
    if name not in DEFAULTS:
        return None
    raw = database.get_value(TABLE, _KEY.format(name))
    return raw if raw is not None else DEFAULTS[name]


def set_param(name: str, value) -> None:
    """Сохранить параметр (с округлением для чисел с плавающей точкой)."""
    if name not in DEFAULTS:
        return
    try:
        if name in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            value = round(float(value), 4)
        else:
            value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректное значение для «{name}»")
    database.upsert(TABLE, _KEY.format(name), value)


def reset(name: str) -> None:
    """Вернуть параметр к значению по умолчанию (None = удалить ключ)."""
    if name not in DEFAULTS:
        return
    database.upsert(TABLE, _KEY.format(name), None)


def base_kwargs() -> dict:
    """Параметры для chat.completions.create (уже без None).
    max_tokens подаём только если отличается от 0."""
    kwargs = {}
    for name, default in DEFAULTS.items():
        val = get(name)
        if val is None:
            continue
        if name in ("frequency_penalty", "presence_penalty") and float(val) == 0.0:
            continue
        if name == "top_p" and float(val) == 1.0:
            continue
        if name == "temperature" and float(val) == 1.0:
            continue
        if name == "max_tokens" and int(val) <= 0:
            continue
        kwargs[name] = val
    return kwargs


def describe() -> str:
    """Человекочитаемая сводка текущих параметров для админ-панели."""
    return "\n".join(f"• {k}: <b>{get(k)}</b>" for k in DEFAULTS)


def name_of(key: str) -> str:
    labels = {
        "temperature": "Температура",
        "top_p": "Top-P",
        "max_tokens": "Max токенов",
        "frequency_penalty": "Штраф повторов",
        "presence_penalty": "Штраф новизны",
    }
    return labels.get(key, key)
