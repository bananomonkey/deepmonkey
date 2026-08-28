"""База знаний об участниках чата: имена, портреты и локальные кликухи.

Собирается из экспортированной истории (member_log_all) и встраивается в
контекст групповых ответов, чтобы бот знал, кто есть кто в чате, понимал
локальные мемы и обращался к людям по именам/кличкам.
"""
import asyncio
import json
import logging
import re
from typing import Dict, List, Optional

import database

logger = logging.getLogger(__name__)

# Сколько сообщений нужно участнику, чтобы строить по нему портрет.
MIN_MESSAGES_FOR_PORTRAIT = 15
# Сколько сообщений брать для портрета.
PORTRAIT_MESSAGE_LIMIT = 250
# Раз в сколько секунд повторно строить портреты (мутабельная личность).
REBUILD_AFTER_SECONDS = 3600 * 24 * 3
# Сколько портретов строить за один проход (для контроля расхода токенов).
MAX_PORTRAITS_PER_BUILD = 50

# Стоп-слова, которые не считаем кликухами при анализе частых коротких слов.
_STOPWORDS = {
    "а", "и", "но", "да", "нет", "не", "ни", "как", "так", "что", "кто", "где",
    "когда", "почему", "зачем", "это", "то", "он", "она", "они", "мы", "вы",
    "ты", "я", "меня", "себя", "тебя", "все", "всё", "его", "её", "их", "если",
    "бы", "был", "была", "были", "будет", "буду", "есть", "быть", "ну",
    "вот", "уже", "ещё", "еще", "тоже", "только", "очень", "можно", "надо",
    "хочешь", "хочу", "давай", "сейчас", "потом", "нужно", "просто",
    "короче", "блин", "привет", "пока", "спс", "пж", "плиз", "с", "по", "на",
    "из", "за", "от", "до", "при", "про", "бля", "блять", "хуй", "че", "чего",
    "какой", "какая", "какие", "такой", "такая", "такие", "этот", "эта",
    "эти", "тот", "та", "те", "мне", "мной", "нас", "вас", "их", "её", "ему",
    "им", "ним", "ры", "де", "там", "тут", "здесь", "или", "было",
    "который", "которая", "которые", "чтобы", "вообще", "теперь",
    "тогда", "сюда", "туда", "сука", "делать", "же", "ж", "весь", "вся",
    "сам", "сама", "сами", "другой", "другая", "другие", "раз", "два",
    "три", "типо", "типа", "говорит", "сказал", "сказать", "могу",
    "можешь", "аж", "наверное", "конечно",
    "ладно", "ага", "нее", "угу", "ппц", "капец", "ща", "щас",
    "пошел", "пошёл", "иди", "ебать", "нах", "нахер", "нахуй",
    "пиздец", "пизда", "еблан", "даун", "чмо", "чмошник", "лох", "дебил",
    "хуйня", "херня", "такое", "какое",
}
def _extract_texts_for_user(user_id, limit: int) -> List[str]:
    """Текстовые сообщения участника из всех экспортированных чатов."""
    return [
        (m.get("text") or "").strip()
        for m in database.get_member_log_all_for_user_global(user_id, limit=limit)
        if m.get("text")
    ]


def _member_name(user_id) -> str:
    """Отображаемое имя участника из экспорта."""
    for _cid, messages in database.iter_member_log_all_chats():
        for m in messages:
            if str(m.get("user_id")) == str(user_id):
                n = m.get("name") or m.get("username")
                if n:
                    return str(n)
    return str(user_id)


def collect_chat_nicknames(chat_messages) -> List[str]:
    """Собрать локальные кликухи/сленг чата — частые короткие слова.

    Проходим по всем текстам экспорта, считаем короткие (3-12 симв.) слова в
    нижнем регистре, убираем стоп-слова, оставляем самые частые.
    """
    counter: Dict[str, int] = {}
    pat = re.compile(r"[а-яёa-z]{3,12}", re.IGNORECASE)
    for m in chat_messages:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        for w in pat.findall(text):
            wl = w.lower()
            if wl in _STOPWORDS:
                continue
            counter[wl] = counter.get(wl, 0) + 1
    # Порог: встречается достаточно часто, чтобы считаться кликухой.
    total = sum(counter.values()) or 1
    threshold = max(5, int(total * 0.0002))
    ranked = sorted((w for w, c in counter.items() if c >= threshold),
                    key=lambda w: -counter[w])
    # Ограничиваем до 60 самых частых кликух.
    return ranked[:60]


async def build_member_knowledge(force: bool = False) -> Dict[str, dict]:
    """(Пере)собрать базу знаний об участниках из экспорта.

    Для каждого участника с достаточным числом сообщений строит/обновляет
    портрет (если его ещё нет или он устарел; при force=True — всегда) и имя.
    Также обновляет локальные кликухи каждого чата. Возвращает {user_id: record}.
    """
    from deepseek_client import describe_personality

    # Новые данные экспорта — сбрасываем кэш индекса поиска.
    _reset_search_cache()
    # При принудительном обновлении сбрасываем и кэш портретов, чтобы выдать свежие.
    if force:
        database.clear_personality_cache()

    # Сначала соберём агрегированную статистику по участникам.
    user_texts: Dict[str, List[str]] = {}
    chat_messages: Dict[str, list] = {}
    for chat_id, messages in database.iter_member_log_all_chats():
        chat_messages[str(chat_id)] = messages
        for m in messages:
            uid = str(m.get("user_id"))
            if not uid:
                continue
            text = (m.get("text") or "").strip()
            if text:
                user_texts.setdefault(uid, []).append(text)

    # Обновляем кликухи чатов.
    for cid, messages in chat_messages.items():
        nicknames = collect_chat_nicknames(messages)
        database.set_chat_nicknames(cid, nicknames)

    # Строим/обновляем знания по участникам.
    existing = database.load_member_knowledge()
    knowledge: Dict[str, dict] = dict(existing)

    # Портреты строим конкурентно, но с лимитом, чтобы не перегрузить API.
    # Сначала зафиксируем имена/число сообщений для всех участников, а портреты
    # будем генерировать максимум для MAX_PORTRAITS_PER_BUILD самых активных,
    # у которых портрета ещё нет или он устарел.
    pending: List[str] = []
    for uid in user_texts:
        texts = user_texts[uid]
        rec = existing.get(uid) or {}
        name = _member_name(uid)
        portrait = rec.get("portrait", "") or ""
        age = rec.get("updated", 0)
        if portrait and not force and ((time_now() - age) < REBUILD_AFTER_SECONDS):
            # Портрет свежий — сохраняем как есть.
            knowledge[uid] = {
                "name": rec.get("name") or name,
                "portrait": portrait,
                "nicknames": rec.get("nicknames", []),
                "message_count": len(texts),
                "updated": age,
            }
        elif len(texts) >= MIN_MESSAGES_FOR_PORTRAIT:
            pending.append(uid)
            # Записываем имя/счётчик сразу (портрет добъём ниже или в след. раз).
            knowledge[uid] = {
                "name": name, "portrait": portrait, "nicknames": [],
                "message_count": len(texts), "updated": age,
            }

    # Только самые активные среди тех, кому нужен портрет.
    pending.sort(key=lambda u: -len(user_texts[u]))
    pending = pending[:MAX_PORTRAITS_PER_BUILD]

    semaphore = asyncio.Semaphore(5)

    async def build_for(uid: str):
        texts = user_texts.get(uid, [])
        name = _member_name(uid)
        async with semaphore:
            try:
                new_portrait = await describe_personality(name, texts[-PORTRAIT_MESSAGE_LIMIT:])
            except Exception as e:
                logger.error("Не удалось построить портрет %s: %s", name, e)
                new_portrait = knowledge[uid].get("portrait", "") or ""
            knowledge[uid] = {
                "name": name,
                "portrait": new_portrait,
                "nicknames": [],
                "message_count": len(texts),
                "updated": time_now(),
            }

    await asyncio.gather(*(build_for(uid) for uid in pending))

    # Сохраняем в БД.
    for uid, rec in knowledge.items():
        database.set_member_knowledge(
            uid, rec["name"], rec.get("portrait", ""), rec.get("nicknames", []),
            rec.get("message_count", 0),
        )
    logger.info("База знаний об участниках: %d записей", len(knowledge))
    return knowledge


def time_now() -> float:
    import time
    return time.time()


def build_group_context(chat_id: int, user_id: Optional[int] = None, limit: int = 30) -> str:
    """Компактный текст «кто есть кто» для встраивания в групповой системный промт.

    Перечисляет участников чата (имя + кликухи) и, где есть, их краткий портрет.
    user_id отвечающего исключается (не выдумываем про него).
    """
    knowledge = database.load_member_knowledge()
    nicknames = database.get_chat_nicknames(chat_id)

    lines = []
    for uid, rec in knowledge.items():
        if user_id is not None and str(uid) == str(user_id):
            continue
        name = rec.get("name") or str(uid)
        nick = rec.get("nicknames") or []
        portrait = rec.get("portrait") or ""
        if portrait:
            lines.append(f"• {name} (id {uid}): {portrait}")

    text = ""
    if lines:
        text += (
            "Что известно об участниках этого чата (имена, характер, привычки; "
            "используй как контекст, не зачитывай дословно):\n" + "\n".join(lines)
        )
    if nicknames:
        nick_list = ", ".join(nicknames[:25]) or ""
        if nick_list:
            text += ("\n\nЛокальные кликухи/сленг этого чата (могут обозначать "
                     "участников или местные мемы): " + nick_list + ".")
    return text


# --- Поиск по экспортированным данным чата ---
# Когда вопрос явно касается людей чата или содержимого переписки, бот ищет
# релевантные сообщения в экспорте и подставляет их как контекст (без веб-поиска).

DECIDE_CHAT_SEARCH_PROMPT = (
    "Ты решаешь, относится ли вопрос к переписке/участникам ЭТОГО чата так, что "
    "ответ нужно искать в истории чата, а не в интернете.\n"
    "ОТВЕТЬ ДА если вопрос:\n"
    "- про конкретного участника этого чата (по имени/кликухе/username/id): "
    "'кто такой X', 'что X говорил', 'о ком шла речь', 'про кого он/она'\n"
    "- про локальный мем, кликуху, сленг или событие, которое было в этом чате\n"
    "- 'что обсуждали', 'что сказал X', 'помнишь, было...' и т.п.\n\n"
    "ОТВЕТЬ НЕТ если вопрос общих знаний/фактов/новостей/математики/кода "
    "(тогда ищем в интернете).\n\n"
    "Ответь ровно одной строкой: ДА или НЕТ."
)


async def ai_decides_chat_search(user_text: str) -> bool:
    """Спросить ИИ, надо ли искать ответ в истории чата."""
    from deepseek_client import client
    from model_manager import model_manager
    try:
        resp = await client.chat.completions.create(
            model=model_manager.get(),
            messages=[
                {"role": "system", "content": DECIDE_CHAT_SEARCH_PROMPT},
                {"role": "user", "content": user_text},
            ],
            timeout=15,
        )
        answer = (resp.choices[0].message.content or "").strip().upper()
        return answer.startswith("ДА")
    except Exception as e:
        logger.error("Ошибка решения о поиске по чату: %s", e)
        return False


def search_exported_messages(query: str, chat_id=None, limit: int = 15) -> str:
    """Найти сообщения из экспорта, релевантные запросу.

    Ищем по ключевым словам запроса (имена/кликухи, с учётом склонений),
    возвращаем красивый отрывок переписки в виде «имя: текст». Если chat_id
    задан — только этот чат, иначе по всем экспортированным.
    """
    # Ключевые токены: упомянутые имена/@username/id и значимые слова запроса.
    tokens = render_query_terms(query)
    if not tokens:
        return ""

    results = []
    for cid, messages in database.iter_member_log_all_chats():
        if chat_id is not None and str(cid) != str(chat_id):
            continue
        for m in messages:
            text = (m.get("text") or "").strip()
            if not text:
                continue
            name = m.get("name") or (m.get("username") or "") or str(m.get("user_id"))
            hay = f"{name} {text}".lower()
            if any(tok in hay for tok in tokens):
                results.append(f"{name}: {text[:500]}")
    # Самое релевантное — последние совпадения.
    return "\n".join(results[-limit:])



# ---------------------------------------------------------------------------
#  Поиск по экспорту с учётом склонений/транслита
#  Строим индекс «отличительных» терминов переписки (клички, имена, сленг),
#  а запросные слова фаззи-сопоставляем с ними (чтобы «лысого» находил
#  «лысый», «вастика» — «вастик» и т.п.).
# ---------------------------------------------------------------------------

# Порог «отличительности»: слово встречается не чаще этого числа сообщений —
# значит это скорее кличка/имя/местный сленг, а не общая лексика.
DISTINCTIVE_MAX = 400
# Сколько фаззи-кандидатов проверяем на одно слово (защита от слишком больших корзин).
MAX_BUCKET_CHECK = 300
_CACHE_TTL = 600  # 10 минут

_TRANS = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit_lat(s: str) -> str:
    out = []
    for ch in str(s).lower():
        if "а" <= ch <= "я" or ch == "ё":
            out.append(_TRANS.get(ch, ch))
        else:
            out.append(ch)
    return "".join(out)


def _norm_word(s: str) -> str:
    """Нижний регистр, без спецсимволов, буквы любого алфавита + цифры."""
    return re.sub(r"[^a-zа-яё0-9]", "", str(s).lower())


def _loose_match(a: str, b: str) -> bool:
    """Слабое совпадение двух нормализованных слов (фаззи + общий префикс)."""
    if not a or not b:
        return False
    if a == b:
        return True
    import difflib
    # Общий префикс 3 символов — грубое, но дешёвое совпадение склонений.
    if len(a) >= 3 and len(b) >= 3 and a[:3] == b[:3]:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


_SEARCH_INDEX = None
_SEARCH_INDEX_TS = 0.0


def _build_search_index(refresh: bool = False):
    """Однопроходно построить индекс поиска по экспорту.

    terms — все «искомые» термины (отличительные слова + имена участников),
    prefix_map — word[:3] -> [terms...] для быстрого фаззи-расширения.
    """
    global _SEARCH_INDEX, _SEARCH_INDEX_TS
    now = time_now()
    if _SEARCH_INDEX and not refresh and (now - _SEARCH_INDEX_TS) < _CACHE_TTL:
        return _SEARCH_INDEX

    counter = {}
    member_names = set()
    for _cid, messages in database.iter_member_log_all_chats():
        for m in messages:
            fr = m.get("from") or m.get("username") or ""
            n = _norm_word(fr)
            if n and len(n) >= 3:
                member_names.add(n)
                member_names.add(_translit_lat(n))
            t = m.get("text")
            if isinstance(t, str) and t.strip():
                for w in re.findall(r"[а-яёa-z]{3,}", t.lower()):
                    if w in _STOPWORDS:
                        continue
                    counter[w] = counter.get(w, 0) + 1

    terms = set(member_names)
    for w, c in counter.items():
        if c <= DISTINCTIVE_MAX:
            terms.add(w)
            terms.add(_translit_lat(w))

    prefix_map = {}
    for w in terms:
        key = w[:3]
        prefix_map.setdefault(key, []).append(w)

    _SEARCH_INDEX = {"terms": terms, "prefix_map": prefix_map}
    _SEARCH_INDEX_TS = now
    return _SEARCH_INDEX


def _reset_search_cache() -> None:
    global _SEARCH_INDEX, _SEARCH_INDEX_TS
    _SEARCH_INDEX = None
    _SEARCH_INDEX_TS = 0.0


def render_query_terms(query: str) -> List[str]:
    """Вернуть список терминов переписки, соответствующих запросу.

    Для каждого слова/хэндла/id запроса ищет совпадение среди отличительных
    терминов экспорта (с учётом склонений и транслита).
    """
    q = str(query or "")
    idx = _build_search_index()
    prefix_map = idx["prefix_map"]

    found = set()

    for h in re.finditer(r"@([A-Za-z0-9_]+)", q):
        found.add(h.group(1).lower())
    for u in re.finditer(r"user(\d+)", q, re.IGNORECASE):
        found.add(u.group(1))

    words = [w for w in re.findall(r"[а-яёa-z]{3,}", q.lower()) if w not in _STOPWORDS]
    for w in words:
        # Точное совпадение с термином или его транслитом.
        if w in idx["terms"] or _translit_lat(w) in idx["terms"]:
            found.add(w)
            continue
        # Фаззи по корзине общего префикса (склонения/опечатки).
        wl = _translit_lat(w)
        for prefix in {w[:3], wl[:3]}:
            bucket = prefix_map.get(prefix)
            if not bucket:
                continue
            for cand in bucket[:MAX_BUCKET_CHECK]:
                if _loose_match(w, cand):
                    found.add(cand)
    return sorted(found)


def query_refers_to_chat_member(query: str) -> bool:
    """Есть ли в запросе обращение к человеку/кликухе из переписки чата.

    Детерминированная проверка (без AI): если какие-то слова запроса
    соответствуют отличительным терминам экспорта (клички, имена, сленг) —
    значит вопрос про чат и стоит искать ответ в переписке.
    """
    return bool(render_query_terms(query))


