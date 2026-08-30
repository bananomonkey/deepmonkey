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
# Реестр инструментов (схемы для OpenAI/DeepSeek tools)
# ---------------------------------------------------------------------------
def as_tools_schema() -> list:
    """Вернуть список схем в формате OpenAI `tools` для передачи в API."""
    return [
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
    ]


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
