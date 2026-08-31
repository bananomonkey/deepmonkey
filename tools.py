"""Набор API-инструментов (function calling) для DeepSeek.

Каждый инструмент = пара:
- *схема* (формат OpenAI/DeepSeek `tools`): name, description, parameters.
- *исполнитель* (`async def _run_xxx(**kwargs) -> str`): возвращает строку-результат
  для модели.

Все инструменты бесплатные и не требуют ключей. Их вызывает сама модель, когда
пользователь спрашивает про время, погоду, курсы, праздники, аниме, покемонов,
шуточки, фейковые профили, QR/аватарки и т.п.
"""

import asyncio
import logging
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 DeepseekBot"
    ),
}

# Часовой пояс по умолчанию (можно переопределить через env/панель).
TIMEZONE = "Europe/Moscow"


# ---------------------------------------------------------------------------
# Вспомогательные утилиты
# ---------------------------------------------------------------------------
def _get_json(url: str, **params):
    """GET -> JSON-словарь (или None при любой ошибке/не 2xx)."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=_HEADERS) as client:
            resp = client.get(url, params=params or None)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("TOOL GET %s : %s", url, e)
        return None


async def _get_json_async(url: str, timeout: int = _HTTP_TIMEOUT, **params):
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_HEADERS) as client:
            resp = await client.get(url, params=params or None)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("TOOL GET async %s : %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Инструменты: время, погода, финансы, праздники
# ---------------------------------------------------------------------------
async def _run_get_current_time(tz: str = TIMEZONE) -> str:
    data = await _get_json_async(
        "https://timeapi.io/api/Time/current/zone", timeZone=tz
    )
    if not data:
        return "timeapi.io недоступен. Попробуй позже."
    date = data.get("dateTime", "")
    return (
        f"Сейчас {date} ({tz}). "
        f"День недели: {data.get('dayOfWeek', '?')}. "
        f"Дата: {data.get('day', '?')}.{data.get('month', '?')}.{data.get('year', '?')}."
    )


_WMO = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "лёгкая морось", 53: "морось",
    55: "сильная морось", 61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь", 71: "небольшой снег",
    73: "снег", 75: "сильный снег", 77: "снежная крупа", 80: "небольшой ливень",
    81: "ливень", 82: "сильный ливень", 85: "снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}


async def _run_get_weather(city: str) -> str:
    geo = await _get_json_async(
        "https://geocoding-api.open-meteo.com/v1/search",
        name=city, count=1, language="ru", format="json",
    )
    results = (geo or {}).get("results") or []
    if not results:
        return f"Не удалось найти город «{city}»."
    place = results[0]
    lat, lon = place.get("latitude"), place.get("longitude")
    local_name = place.get("name") or city
    country = place.get("country") or ""
    data = await _get_json_async(
        "https://api.open-meteo.com/v1/forecast",
        latitude=lat, longitude=lon,
        current="temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
    )
    if not data:
        return f"Не удалось получить погоду для города «{local_name}»."
    cur = (data.get("current") or {})
    code = cur.get("weather_code")
    desc = _WMO.get(code, f"код {code}")
    lines = [
        f"Погода в {local_name} ({country}): {desc}.",
        f"Температура: {cur.get('temperature_2m')}°C (ощущается как {cur.get('apparent_temperature')}°C).",
        f"Влажность: {cur.get('relative_humidity_2m')}%. Ветер: {cur.get('wind_speed_10m')} км/ч.",
    ]
    return "\n".join(lines)


async def _run_get_currency_rate(currency: str = "USD") -> str:
    data = await _get_json_async("https://www.cbr-xml-daily.ru/daily_json.js")
    if not data:
        return "Курсы ЦБ временно недоступны."
    valute = data.get("Valute") or {}
    cur = currency.upper()
    if cur in valute:
        item = valute[cur]
        return f"Курс {item.get('Name')} ({cur}) = {item.get('Value')} руб. (номинал {item.get('Nominal')}). Дата: {data.get('Date')}."
    # Возвращаем доступные валюты
    names = ", ".join(list(valute.keys())[:12])
    return f"Валюта «{currency}» не найдена. Доступные коды: {names}."


async def _run_get_crypto_price(coin: str = "bitcoin") -> str:
    data = await _get_json_async(
        "https://api.coingecko.com/api/v3/simple/price",
        ids=coin, vs_currencies="usd",
    )
    if not data:
        return "CoinGecko недоступен или монета не найдена."
    if coin in data:
        return f"{coin} = ${data[coin].get('usd')}."
    keys = ", ".join(list(data.keys()))
    return f"Монета «{coin}» не найдена. Известные: {keys} (запрос по id, напр. bitcoin/ethereum)."


async def _run_get_public_holidays(year: int = 0, country: str = "RU") -> str:
    import datetime
    if not year:
        year = datetime.datetime.now().year
    data = await _get_json_async(
        f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country.upper()}"
    )
    if not isinstance(data, list):
        return f"Праздники для {country.upper()} ({year}) недоступны."
    if not data:
        return f"Праздников для {country.upper()} в {year} не найдено."
    lines = [f"Праздники {country.upper()} в {year}:"]
    for h in data:
        lines.append(f"- {h.get('date')}: {h.get('localName')}")
    return "\n".join(lines)[:1500]


# ---------------------------------------------------------------------------
# Инструменты: поиск, чтение ссылок
# ---------------------------------------------------------------------------
async def _run_search_web(query: str) -> str:
    # Прямая реализация через duckduckgo_search (бесплатно, без ключа).
    # Если она недоступна — лениво просим поиск из deepseek_client.
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS(timeout=_HTTP_TIMEOUT) as ddgs:
            for r in ddgs.text(query, max_results=5):
                results.append(
                    f"{r.get('title')}\n{r.get('body')}\n{r.get('href')}"
                )
        if results:
            return "\n\n".join(results)
        return "Ничего не найдено по запросу."
    except Exception as e:
        logger.error("TOOL search_web ddgs: %s", e)
    try:
        from deepseek_client import web_search
        return await web_search(query, max_results=5)
    except Exception as e:
        logger.error("TOOL search_web fallback: %s", e)
        return "Поиск временно недоступен."


async def _run_read_url(url: str) -> str:
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Допустимы только ссылки http/https."
    import html as _html
    import re as _re
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers=_HEADERS) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return f"Не удалось прочитать страницу (код {resp.status_code})."
            raw = resp.text
    except Exception as e:
        logger.error("TOOL read_url fetch: %s", e)
        return "Не удалось скачать страницу."
    # Убираем скрипты/стили и помечаем видимые блочные теги переносами.
    raw = _re.sub(r"(?is)<(script|style|noscript|head)[^>]*>.*?</\1>", " ", raw)
    raw = _re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr|/table)[^>]*>", "\n", raw)
    raw = _re.sub(r"<[^>]+>", " ", raw)
    text = _html.unescape(raw)
    text = _re.sub(r"[ \t\u00a0]+", " ", text)
    text = _re.sub(r"\n\s*\n+", "\n", text).strip()
    if not text:
        return "Страница пустая или полностью на JavaScript."
    return text[:8000]


# ---------------------------------------------------------------------------
# Инструменты: развлечения/генерация
# ---------------------------------------------------------------------------
async def _run_tell_joke() -> str:
    data = await _get_json_async("https://v2.jokeapi.dev/joke/Any")
    if not data:
        return "Не удалось получить шутку."
    if data.get("type") == "twopart":
        return f"{data.get('setup')}\n\n{data.get('delivery')}"
    if data.get("joke"):
        return data["joke"]
    return "Шутка не найдена."


async def _run_make_qr(url: str) -> str:
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Для QR нужна корректная ссылка (http/https)."
    img_url = ("https://api.qrserver.com/v1/create-qr-code/"
               f"?size=200x200&data={urllib.parse.quote(url)}")
    return f"[QR-код по ссылке]({img_url}) | QRIMG={img_url} | для {url}"


async def _run_make_avatar(seed: str) -> str:
    img_url = (
        "https://api.dicebear.com/9.x/bottts/svg?seed="
        + urllib.parse.quote(seed)
        + "&backgroundColor=transparent"
    )
    return f"[Аватарка-робот по слову «{seed}»]({img_url}) | AVATARIMG={img_url}"


async def _run_random_user() -> str:
    data = await _get_json_async("https://randomuser.me/api/")
    results = data.get("results") if data else []
    if not results:
        return "RandomUser недоступен."
    u = results[0]
    name = u.get("name", {})
    loc = u.get("location", {})
    street = loc.get("street", {})
    dob = u.get("dob", {})
    pic = (u.get("picture") or {}).get("large", "")
    lines = [
        f"Вымышленный профиль:",
        f"- Имя: {name.get('title')} {name.get('first')} {name.get('last')}",
        f"- Пол: {u.get('gender')}",
        f"- Email: {u.get('email')}",
        f"- Телефон: {u.get('phone')}",
        f"- Адрес: {street.get('number')} {street.get('name')}, {loc.get('city')}, {loc.get('state')}, {loc.get('country')}, {loc.get('postcode')}",
        f"- Дата рождения: {''.join(str(dob.get('date',''))[:10])}",
        f"- Национальность: {u.get('nat')}",
    ]
    if pic:
        lines.append(f"- Фото: {pic}")
        lines.append(f"| RANDOMUSERIMG={pic}")
    return "\n".join(lines)


async def _run_anime_search(query: str) -> str:
    data = await _get_json_async(
        "https://api.jikan.moe/v4/anime", q=query, limit=3
    )
    if not data:
        return "Jikan/MyAnimeList временно недоступен (504). Попробуй позже."
    found = data.get("data") or []
    if not found:
        return f"По запросу «{query}» аниме не найдено."
    lines = [f"Аниме по запросу «{query}»:"]
    for a in found:
        title = a.get("title")
        year = (a.get("aired") or {}).get("from", "")[:4] or "?"
        score = a.get("score")
        episodes = a.get("episodes")
        lines.append(
            f"- {title} ({year}), рейтинг {score}, серий: {episodes}"
        )
    return "\n".join(lines)


async def _run_pokemon_info(name: str) -> str:
    data = await _get_json_async(
        f"https://pokeapi.co/api/v2/pokemon/{urllib.parse.quote(name.lower())}"
    )
    if not data:
        return f"Покемон «{name}» не найден."
    types = ", ".join(t["type"]["name"] for t in data.get("types", []))
    abilities = ", ".join(a["ability"]["name"] for a in data.get("abilities", []))
    return (
        f"{data.get('name')}: тип(ы): {types}.\n"
        f"Вес: {data.get('weight')/10} кг, рост: {data.get('height')/10} м.\n"
        f"Базовый опыт: {data.get('base_experience')}.\n"
        f"Способности: {abilities}. ID: {data.get('id')}."
    )


# ---------------------------------------------------------------------------
# Инструменты: песочница кода, статусы, математика, UUID, факты
# ---------------------------------------------------------------------------
_PISTON_RUNNERS = "https://emkc.org/api/v2/piston/runtimes"
_PISTON_EXECUTE = "https://emkc.org/api/v2/piston/execute"


async def _run_code_execute(code: str, language: str = "python") -> str:
    """Запустить код в изолированной песочнице Piston и вернуть вывод."""
    lang = (language or "python").lower().strip()
    try:
        if not code or not code.strip():
            return "Пустой код — нечего выполнять."
    except Exception:
        return "Ошибка чтения кода."

    try:
        async with httpx.AsyncClient(timeout=30, headers=_HEADERS) as client:
            r = await client.get(_PISTON_RUNNERS)
            r.raise_for_status()
            runtimes = r.json()
    except Exception as e:
        logger.error("Piston runtimes: %s", e)
        return "Песочница кода недоступна."

    version = None
    for rt in runtimes or []:
        if str(rt.get("language", "")).lower() == lang:
            version = rt.get("version")
            break
    if version is None:
        available = ", ".join(str(rt.get("language")) for rt in (runtimes or [])[:20])
        return f"Язык «{language}» не поддерживается. Доступны (первые): {available}."

    payload = {"language": lang, "version": version, "files": [{"content": code}]}
    try:
        async with httpx.AsyncClient(timeout=30, headers=_HEADERS) as client:
            resp = await client.post(_PISTON_EXECUTE, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error("Piston execute: %s", e)
        return "Не удалось выполнить код (песочница недоступна)."

    run = data.get("run", {})
    out = (run.get("output") or "").strip()
    stderr = (run.get("stderr") or "").strip()
    exit_code = run.get("code")
    parts = [f"Язык: {lang} {version}. Код возврата: {exit_code}."]
    if out:
        parts.append(f"Вывод:\n{out}")
    if stderr:
        parts.append(f"Ошибки (stderr):\n{stderr}")
    if not out and not stderr:
        parts.append("(нет вывода)")
    return "\n\n".join(parts)[:4000]


async def _run_http_cat(code: int = 404) -> str:
    """Вернуть картинку кота по HTTP-статусу (ссылкой)."""
    code = int(code) if str(code).isdigit() else 404
    img_url = f"https://http.cat/{code}.jpg"
    return f"[Кот для HTTP-статуса {code}]({img_url}) | CATIMG={img_url}"


async def _run_site_status(url: str) -> str:
    """Проверить доступность сайта (пинг хоста)."""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers=_HEADERS) as client:
            resp = await client.get(url)
        return (
            f"{url}\nСтатус: {resp.status_code}. "
            f"Время ответа: {resp.elapsed.total_seconds():.2f} с."
        )
    except httpx.TimeoutException:
        return f"{url}\nТаймаут — сайт не отвечает или лежит."
    except Exception as e:
        return f"{url}\nНе удалось получить доступ: {type(e).__name__}."


_NEWTON_OPS = {
    "simplify", "factor", "derive", "integrate", "zeroes", "tangent", "area",
    "cos", "sin", "tan", "arccos", "arcsin", "arctan", "absolute", "log",
}


async def _run_math_newton(op: str = "simplify", expr: str = "") -> str:
    """Решить математическое выражение через Newton API."""
    op = (op or "").lower().strip()
    if op not in _NEWTON_OPS:
        return f"Неизвестная операция «{op}». Доступны: {', '.join(sorted(_NEWTON_OPS))}."
    if not (expr or "").strip():
        return "Не передано выражение."
    url = f"https://api.newton.af/v2/{op}/{urllib.parse.quote(expr)}"
    data = await _get_json_async(url)
    if not data:
        return "Newton API недоступен или выражение не удалось обработать."
    return (
        f"Операция: {data.get('operation')}. "
        f"Выражение: {data.get('expression')}.\nРезультат: {data.get('result')}"
    )


async def _run_make_uuid() -> str:
    """Сгенерировать случайный UUID v4."""
    import uuid
    return str(uuid.uuid4())


_hp_urls = {
    "characters": "https://hp-api.onrender.com/api/characters",
    "students": "https://hp-api.onrender.com/api/characters/students",
    "spells": "https://hp-api.onrender.com/api/spells",
    "houses": "https://hp-api.onrender.com/api/houses",
}


async def _run_hp_info(category: str = "characters", query: str = "") -> str:
    """Данные по вселенной Гарри Поттера: персонажи, заклинания, факультеты."""
    cat = (category or "characters").lower().strip()
    url = _hp_urls.get(cat, _hp_urls["characters"])
    data = await _get_json_async(url)
    if not isinstance(data, list):
        return "API Гарри Поттера недоступен."
    if (query or "").strip():
        q = query.strip().lower()
        data = [x for x in data if q in str(x.get("name", "")).lower()]
    if not data:
        return "Ничего не найдено."
    lines = []
    for item in data[:3]:
        name = item.get("name") or "?"
        if cat in ("characters", "students"):
            house = item.get("house") or "—"
            lines.append(f"• {name} — факультет: {house}")
        elif cat == "spells":
            desc = item.get("description") or "—"
            lines.append(f"• {name} — {desc}")
        else:
            lines.append(f"• {name}")
    if len(data) > 3:
        lines.append(f"... и ещё {len(data)-3} записей.")
    return "\n".join(lines)[:1500]


async def _run_cat_fact() -> str:
    """Случайный факт о котах (англ.) для пересказа пользователю."""
    data = await _get_json_async("https://catfact.ninja/fact")
    if not data or not data.get("fact"):
        return "Не удалось получить факт о котах."
    return "🐱 Факт о котах (на английском — переведи пользователю на русский):\n" + data["fact"]


# ---------------------------------------------------------------------------
# Реестр инструментов (схемы для OpenAI/DeepSeek tools)
# ---------------------------------------------------------------------------
def as_tools_schema() -> list:
    """Вернуть список схем в формате OpenAI `tools` для передачи в API."""
    return _TOOL_SCHEMAS


def as_gemini_tool_schema() -> list:
    """Вернуть те же инструменты в формате Gemini `functionDeclarations`.

    Gemini использует формат {name, description, parameters} вместо
    OpenAI-овского {"type": "function", "function": {...}}.
    """
    declarations = []
    for schema in _TOOL_SCHEMAS:
        fn = schema.get("function", {})
        declarations.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return declarations


_TOOL_SCHEMAS: list = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Узнать текущие дату и время (и день недели). Используй, когда спрашивают 'который час', 'какое сегодня число', 'какой день недели'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tz": {"type": "string", "description": "Часовой пояс, напр. Europe/Moscow", "default": TIMEZONE}
                },
            },
        },
    },
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Узнать погоду в городе (температура, влажность, ветер). Используй на вопросы 'какая погода', 'сколько градусов', 'холодно ли'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "Название города (на русском или английском)"}
                    },
                    "required": ["city"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_currency_rate",
                "description": "Узнать официальный курс ЦБ РФ валюты к рублю (USD, EUR и др.). На вопросы 'курс доллара', 'сколько стоит евро в рублях'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "currency": {"type": "string", "description": "Код валюты, напр. USD, EUR, CNY", "default": "USD"}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_crypto_price",
                "description": "Узнать цену криптовалюты в долларах (bitcoin, ethereum и т.п.). На вопросы 'сколько стоит биткоин', 'курс эфира'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "coin": {"type": "string", "description": "ID монеты, напр. bitcoin, ethereum, dogecoin", "default": "bitcoin"}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_public_holidays",
                "description": "Узнать официальные государственные праздники/выходные в стране. На вопросы 'какой сегодня праздник', 'выходной ли', 'когда праздники'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "year": {"type": "integer", "description": "Год (по умолчанию текущий)"},
                        "country": {"type": "string", "description": "Код страны, напр. RU, BY, KZ", "default": "RU"}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "Актуальный веб-поиск по интернету. Используй для новостей, актуальных фактов, событий, всего, чего может не знать модель.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Поисковый запрос"}
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_url",
                "description": "Прочитать содержимое страницы по ссылке и вернуть его текстом. Используй, когда пользователь прислал ссылку и просит пересказать/прочитать.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Полная ссылка http/https на страницу"}
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "make_qr",
                "description": "Сгенерировать QR-код по ссылке. На просьбы 'сделай QR', 'QR-код на...'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Ссылка, для которой нужен QR-код"}
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "make_avatar",
                "description": "Сгенерировать уникальную аватарку-робота по любому слову/тексту (seed). На просьбы 'сделай аватарку', 'придумай картинку профиля'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seed": {"type": "string", "description": "Слово/текст, по которому сгенерировать аватарку"}
                    },
                    "required": ["seed"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tell_joke",
                "description": "Рассказать шутку/анекдот. На просьбы 'пошути', 'расскажи анекдот', 'пошути-ка', 'расскажи шутку'.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "anime_search",
                "description": "Найти информацию по аниме/манге (название, год, рейтинг, серии). На вопросы 'расскажи про аниме', 'что за аниме'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Название аниме для поиска"}
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "pokemon_info",
                "description": "Узнать характеристики покемона (типы, способности, вес, рост, эволюция). На вопросы 'какой покемон', 'данные по покемону'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Имя покемона, напр. pikachu"}
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "random_user",
                "description": "Сгенерировать случайный вымышленный профиль (имя, адрес, email, телефон, фото). На просьбы 'придумай профиль', 'сгенерируй случайного пользователя', 'дай фейк-данные'.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "code_execute",
                "description": "Выполнить код в песочнице и вернуть результат (вывод/ошибки). На просьбы 'выполни код', 'запусти python/js/c++', 'что выведет эта программа'. Поддерживает десятки языков.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Исходный код для выполнения"},
                        "language": {"type": "string", "description": "Язык программирования, напр. python, javascript, cpp", "default": "python"}
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "http_cat",
                "description": "Вернуть картинку кота для HTTP-статуса (код 100-599). Для мемного оформления ошибок и статусов. Картинка возвращается ссылкой.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "integer", "description": "HTTP-код, напр. 404, 500, 418, 200", "default": 404}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "site_status",
                "description": "Проверить, доступен ли сайт (пинг хоста, код и время ответа). На вопросы 'проверь, лежит ли ВК/сайт', 'доступен ли xxx'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Адрес сайта, напр. vk.com или https://example.com"}
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "math_newton",
                "description": "Решить сложный математический пример: упрощение, факторизация, производная, интеграл, корни и т.п. На просьбы 'реши интеграл', 'упрости выражение', 'найди производную', 'чему равно'. Операции: simplify, factor, derive, integrate, zeroes, tangent, area, cos, sin, tan, arccos, arcsin, arctan, absolute, log.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "description": "Операция: simplify, factor, derive, integrate, zeroes, tangent, area, cos, sin, tan, absolute, log и др.", "default": "simplify"},
                        "expr": {"type": "string", "description": "Математическое выражение, напр. x^2-5x+6"}
                    },
                    "required": ["expr"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "make_uuid",
                "description": "Сгенерировать случайный уникальный UUID/GUID. На просьбы 'сгенерируй uuid', 'дай уникальный id', 'случайный хеш'.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hp_info",
                "description": "Данные по вселенной Гарри Поттера: персонажи, студенты, заклинания, факультеты. На вопросы 'расскажи про Хогвартс', 'кто такой Гарри Поттер', 'заклинания'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "Категория: characters, students, spells, houses", "default": "characters"},
                        "query": {"type": "string", "description": "Поиск по имени (необязательно)"}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cat_fact",
                "description": "Получить случайный факт о котах. На просьбы 'расскажи факт о котах', 'интересный факт про кошек'.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def apis_summary() -> str:
    """Человекочитаемый список подключённых API-инструментов с пояснением."""
    lines = []
    name_map = {
        "get_current_time": "🕐 Текущие дата/время",
        "get_weather": "🌦 Погода в городе",
        "get_currency_rate": "💱 Курс валют ЦБ РФ",
        "get_crypto_price": "🪙 Цена криптовалют",
        "get_public_holidays": "🎉 Гос. праздники и выходные",
        "search_web": "🔎 Веб-поиск по интернету",
        "read_url": "🌐 Чтение страницы по ссылке",
        "make_qr": "🔳 Генерация QR-кода",
        "make_avatar": "🤖 Генерация аватарки-робота",
        "tell_joke": "😂 Шутки и анекдоты",
        "anime_search": "🎌 Поиск по аниме/манге",
        "pokemon_info": "⚡ Характеристики покемонов",
        "random_user": "👤 Случайный вымышленный профиль",
        "code_execute": "💻 Песочница кода (Piston)",
        "http_cat": "🐱 Котики по HTTP-статусам",
        "site_status": "📡 Проверка доступности сайта",
        "math_newton": "📐 Математический калькулятор (Newton)",
        "make_uuid": "🔑 Генерация UUID/GUID",
        "hp_info": "🧙 База данных Гарри Поттера",
        "cat_fact": "🐈 Случайные факты о котах",
    }
    for schema in _TOOL_SCHEMAS:
        fn = schema.get("function", {})
        name = fn.get("name", "?")
        desc = fn.get("description", "")
        lines.append(f"• <b>{name_map.get(name, name)}</b> — {desc}")
    return "\n".join(lines)


_EXECUTORS = {
    "get_current_time": _run_get_current_time,
    "get_weather": _run_get_weather,
    "get_currency_rate": _run_get_currency_rate,
    "get_crypto_price": _run_get_crypto_price,
    "get_public_holidays": _run_get_public_holidays,
    "search_web": _run_search_web,
    "read_url": _run_read_url,
    "make_qr": _run_make_qr,
    "make_avatar": _run_make_avatar,
    "tell_joke": _run_tell_joke,
    "anime_search": _run_anime_search,
    "pokemon_info": _run_pokemon_info,
    "random_user": _run_random_user,
    "code_execute": _run_code_execute,
    "http_cat": _run_http_cat,
    "site_status": _run_site_status,
    "math_newton": _run_math_newton,
    "make_uuid": _run_make_uuid,
    "hp_info": _run_hp_info,
    "cat_fact": _run_cat_fact,
}


async def run_tool(name: str, arguments: dict) -> str:
    """Исполнить инструмент по имени. Всегда возвращает строку (никогда не бросает)."""
    fn = _EXECUTORS.get(name)
    if fn is None:
        return f"Неизвестный инструмент: {name}"
    try:
        # Инструменты, которые не принимают аргументов, — вызвать без них.
        if callable(fn):
            return await fn(**arguments) if arguments else await fn()
        return await fn(arguments)
    except Exception as e:
        logger.error("Ошибка инструмента %s: %s", name, e)
        return f"Инструмент {name} вернул ошибку: {e}"


# Для скоординированного исполнения параллельно.
async def run_tools_concurrently(calls: list) -> list:
    """calls: список (name, arguments). Возвращает список строк-результатов."""
    return await asyncio.gather(
        *(run_tool(name, args or {}) for name, args in calls)
    )
