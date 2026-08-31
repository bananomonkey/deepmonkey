import asyncio
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote

import config

logger = logging.getLogger(__name__)

FALLBACK_ANSWER = "⚠️ Произошла ошибка при обращении к нейросети. Попробуйте позже."

PROFILE_SYSTEM_PROMPT = (
    "Ты помогаешь вести краткую служебную заметку о собеседнике для другого "
    "ИИ-ассистента. На основе истории переписки обнови заметку об известных "
    "интересах, предпочтениях и нейтральных фактах о пользователе (только то, "
    "что он сам сообщил о себе). Пиши по-русски, кратко, списком из нескольких "
    "пунктов, без домыслов и оценок. НЕ включай данные повышенной "
    "чувствительности (о здоровье, политических/религиозных взглядах, "
    "ориентации и т.п.), даже если пользователь их упоминал. Не придумывай "
    "факты, которых не было в переписке. Если новой полезной информации "
    "нет — верни заметку без изменений. Ответь только текстом заметки, без "
    "вступлений и пояснений."
)

_search_client = None


def _get_search_client():
    global _search_client
    if _search_client is None:
        try:
            from duckduckgo_search import DDGS
            _search_client = DDGS()
        except ImportError:
            logger.warning("duckduckgo-search не установлен, веб-поиск недоступен")
            return None
    return _search_client


def _format_ddg_results(results) -> str:
    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        parts.append(f"{i}. {title}\n{body}\n{href}")
    return "\n\n".join(parts)


async def _search_duckduckgo(query: str, max_results: int = 5) -> str:
    ddgs = _get_search_client()
    if ddgs is None:
        return ""
    loop = asyncio.get_running_loop()
    for attempt in range(2):
        try:
            results = await loop.run_in_executor(
                None, lambda: list(ddgs.text(query, max_results=max_results))
            )
            if results:
                return _format_ddg_results(results)
        except Exception as e:
            logger.warning("DuckDuckGo попытка %d/2 не удалась: %s", attempt + 1, e)
        await asyncio.sleep(1)
    logger.warning("DuckDuckGo поиск не дал результатов (rate limit?) для: %s", query)
    return ""


async def _search_wikipedia(query: str, max_results: int = 5) -> str:
    import httpx
    headers = {
        "User-Agent": "DeepMonkeyBot/1.0 (https://github.com/bananomonkey/deepmonkey)"
    }
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            resp = await client.get(
                "https://ru.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "format": "json",
                    "srlimit": max_results,
                    "srprop": "snippet",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("query", {}).get("search", [])
            if not hits:
                return ""
            parts = []
            for i, h in enumerate(hits, 1):
                title = h.get("title", "")
                snippet = re.sub(r"<[^>]+>", "", h.get("snippet", ""))
                page_url = f"https://ru.wikipedia.org/wiki/{quote(title)}"
                parts.append(f"{i}. {title} (Википедия)\n{snippet}\n{page_url}")
            return "\n\n".join(parts)
    except Exception as e:
        logger.error("Ошибка поиска в Википедии: %s", e)
        return ""


async def _search_tavily(query: str, max_results: int = 5) -> str:
    if not config.TAVILY_API_KEY:
        return ""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": config.TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "basic",
                    "include_answer": True,
                    "max_results": max_results,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            parts = []
            answer = data.get("answer", "")
            if answer:
                parts.append(f"Краткий ответ Tavily:\n{answer}\n")
            for i, r in enumerate(data.get("results", []), 1):
                title = r.get("title", "")
                content = r.get("content", "")
                url = r.get("url", "")
                parts.append(f"{i}. {title}\n{content}\n{url}")
            return "\n\n".join(parts) if parts else ""
    except Exception as e:
        logger.error("Ошибка поиска Tavily: %s", e)
        return ""


async def web_search(query: str, max_results: int = 5) -> str:
    results = await _search_tavily(query, max_results)
    if results:
        return results
    logger.info("Tavily недоступен, пробую DuckDuckGo для: %s", query)
    results = await _search_duckduckgo(query, max_results)
    if results:
        return results
    logger.info("DuckDuckGo недоступен, пробую Википедию для: %s", query)
    return await _search_wikipedia(query, max_results)


SEARCH_DECISION_PROMPT = (
    "Ты решаешь, нужно ли сделать веб-поиск перед ответом.\n"
    "ИЩИ если есть МАЛЫЙШЕЕ сомнение что ответ может содержать актуальные данные:\n"
    "- факты о людях, компаниях, событиях\n"
    "- новости, погода, курсы, результаты\n"
    "- любая информация которая могла измениться или быть незнаемой\n\n"
    "НЕ ищи ТОЛЬКО если это:\n"
    "- математика, код, объяснение концепций\n"
    "- творческая задача (стик, история, перевод)\n"
    "- общеизвестные факты (столица Франции = Париж)\n\n"
    "Формат ответа — ОДНА строка:\n"
    "SEARCH: <поисковый запрос>\n"
    "или\n"
    "NOSEARCH\n\n"
    "Примеры:\n"
    "«Кто президент Франции?» → SEARCH: президент Франции 2026\n"
    "«Какая погода в Москве?» → SEARCH: погода Москва сегодня\n"
    "«Курс доллара» → SEARCH: курс доллара рубль сегодня\n"
    "«Что нового в ИТ?» → SEARCH: новости IT технологии 2026\n"
    "«Напиши стих» → NOSEARCH\n"
    "«Что такое контейнер?» → NOSEARCH\n"
    "«Объясни async/await» → NOSEARCH\n"
    "Сомневаешься? → ищи. Лучше поискать лишний раз, чем дать неполный ответ."
)


async def _ai_decides_search(
    user_text: str,
    model: Optional[str] = None,
) -> Optional[str]:
    effective_model = model or config.GEMINI_MODEL
    try:
        answer = await gemini_simple_text(
            SEARCH_DECISION_PROMPT, user_text, model=effective_model, timeout=15,
        )
        answer = (answer or "").strip()
        if answer.upper().startswith("SEARCH:"):
            query = answer[len("SEARCH:"):].strip()
            if query:
                logger.info("ИИ решил искать: '%s' (для: %s)", query, user_text[:80])
                return query
        logger.info("ИИ решил не искать: %s", user_text[:80])
        return None
    except Exception as e:
        logger.error("Ошибка при решении о поиске: %s", e)
        return None


# ---------------------------------------------------------------------------
# Gemini низкоуровневый доступ (единая точка, с fallback на второй ключ).
# ---------------------------------------------------------------------------
_BASE = "https://generativelanguage.googleapis.com/v1beta"
_SIMPLE_RETRYABLE = None


async def _gemini_send(
    system_prompt: str,
    messages: List[Dict],
    model: Optional[str] = None,
    images: Optional[List[str]] = None,
    timeout: float = 60,
) -> dict:
    """Один POST к Gemini generateContent с fallback на второй API-ключ.

    Сначала пробуем основной ключ; если он даёт ошибку авторизации/лимит/сбой —
    повторяем тем же запросом со вторым ключом.
    """
    import httpx
    effective_model = model or config.GEMINI_MODEL

    contents: List[Dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            continue
        g_role = "model" if role in ("model", "assistant") else "user"
        if not content:
            continue
        contents.append({"role": g_role, "parts": [{"text": content}]})

    if images:
        parts: List[Dict] = [{"text": messages[-1].get("content", "") if messages else ""}]
        for img in images:
            parts.append(_data_url_to_inline(img))
        if contents:
            contents[-1] = {"role": "user", "parts": parts}
        else:
            contents.append({"role": "user", "parts": parts})

    payload: Dict = {"contents": contents, "generationConfig": {}}
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    keys = [k for k in (config.GEMINI_API_KEY, config.GEMINI_API_KEY_2) if k]

    async def _try(key: str) -> dict:
        url = f"{_BASE}/{effective_model}:generateContent?key={key}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    last_exc = None
    for key in keys:
        try:
            return await _try(key)
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            logger.warning("Gemini API (ключ %s...) ошибка: %s", key[-4:], e)
            continue
    raise last_exc if last_exc else RuntimeError("Нет API-ключей Gemini")


async def gemini_simple_text(
    system_prompt: str,
    user_text: str,
    model: Optional[str] = None,
    timeout: float = 30,
    images: Optional[List[str]] = None,
) -> str:
    """Простой текстовый (и мультимодальный) запрос к Gemini, возвращает строку."""
    try:
        data = await _gemini_send(
            system_prompt,
            [{"role": "user", "content": user_text}],
            model=model, images=images, timeout=timeout,
        )
    except Exception as e:
        logger.error("Gemini simple запрос не удался: %s", e)
        return ""
    return _extract_text(data)


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "\n".join(p.get("text", "") for p in parts if "text" in p).strip()


def _data_url_to_inline(data_url: str) -> Dict:
    if data_url.startswith("data:"):
        header, _, b64 = data_url.partition(",")
        mime = header[5:].split(";")[0] or "image/jpeg"
        return {"mimeType": mime, "data": b64}
    return {"mimeType": "image/jpeg", "data": data_url}


# ---------------------------------------------------------------------------
# Совместимый OpenAI-подобный асинхронный клиент на базе Gemini.
# Позволяет оставить старые вызовы client.chat.completions.create(...) без
# правок (используется в member_knowledge).
# ---------------------------------------------------------------------------
class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoices:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)
        self.choices = [self]


class _FakeCompletions:
    async def create(self, model=None, messages=None, timeout=None, **kwargs):
        system_prompt = ""
        user_text = ""
        for m in messages or []:
            if m.get("role") == "system":
                system_prompt = m.get("content", "")
            else:
                # последнее user-сообщение становится запросом
                user_text = m.get("content", "")
        content = await gemini_simple_text(
            system_prompt, user_text, model=model,
            timeout=timeout if timeout else 30,
        )
        return _FakeChoices(content)


class _FakeChat:
    completions = _FakeCompletions()


class _FakeClient:
    chat = _FakeChat()


# Экспортируем как `client`, чтобы старый код не ломался.
client = _FakeClient()


# ---------------------------------------------------------------------------
# Основной ответ бота: Gemini с агентными инструментами.
# ---------------------------------------------------------------------------
async def ask_deepseek(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
    images: Optional[List[str]] = None,
) -> str:
    from gemini_client import ask_gemini
    return await ask_gemini(
        system_prompt, user_text, history=history,
        model=model, use_thinking=use_thinking, images=images,
    )


async def ask_deepseek_with_search(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
    images: Optional[List[str]] = None,
    chat_context: Optional[str] = None,
) -> str:
    from gemini_client import ask_gemini_with_search
    return await ask_gemini_with_search(
        system_prompt, user_text, history=history,
        model=model, use_thinking=use_thinking, images=images,
        chat_context=chat_context,
    )


async def summarize_profile(old_profile: str, recent_messages: List[Dict[str, str]]) -> str:
    convo_text = "\n".join(f"{m['role']}: {m['content']}" for m in recent_messages)
    user_prompt = (
        f"Текущая заметка о пользователе:\n{old_profile or '(пусто)'}\n\n"
        f"Новые сообщения из переписки:\n{convo_text}\n\n"
        f"Обнови заметку."
    )
    try:
        answer = await gemini_simple_text(
            PROFILE_SYSTEM_PROMPT, user_prompt, timeout=30,
        )
        return answer.strip() if answer else old_profile
    except Exception as e:
        logger.error("Ошибка обновления профиля пользователя: %s", e)
        return old_profile


MEMBER_PROFILE_SYSTEM_PROMPT = (
    "Ты ведёшь краткую служебную заметку об участнике группового чата для "
    "ИИ-ассистента, который тоже в этом чате. На основе сообщений этого "
    "участника составь/обнови компактный профиль: его имя/ник, характер, "
    "интересы, привычки общения, известные факты и отношения с другими "
    "участниками (только то, что видно из переписки). Пиши по-русски, кратко, "
    "списком из нескольких пунктов, без домыслов и оценок. НЕ включай данные "
    "повышенной чувствительности. Если новой полезной информации нет — верни "
    "заметку без изменений. Ответь только текстом заметки, без вступлений."
)


async def summarize_member_profile(
    chat_id: int,
    user_id: int,
    member_name: str,
    old_profile: str,
    recent_messages: List[str],
) -> str:
    """Обновляет заметку об участнике группы на основе его последних сообщений."""
    if not recent_messages:
        return old_profile
    convo_text = "\n".join(f"- {m}" for m in recent_messages)
    user_prompt = (
        f"Участник: {member_name or user_id}\n\n"
        f"Текущая заметка:\n{old_profile or '(пусто)'}\n\n"
        f"Последние сообщения участника из группового чата:\n{convo_text}\n\n"
        f"Обнови заметку об этом участнике."
    )
    try:
        answer = await gemini_simple_text(
            MEMBER_PROFILE_SYSTEM_PROMPT, user_prompt, timeout=30,
        )
        return answer.strip() if answer else old_profile
    except Exception as e:
        logger.error("Ошибка обновления профиля участника группы: %s", e)
        return old_profile


PERSONALITY_SYSTEM_PROMPT = (
    "Ты описываешь личность участника группового чата на основе его сообщений. "
    "Построй развёрнутый, но компактный и объективный портрет на русском: чем "
    "занимается/интересуется, характер и манера общения, темы, которые он часто "
    "поднимает, как относится к другим участникам (только то, что видно из "
    "сообщений). НЕ выдумывай факты, которых нет в сообщениях, не давай оценочных "
    "ярлыков без оснований. Если сообщений мало — честно скажи, что известно "
    "мало. Ответь текстом портрета, без вступлений и лишней воды."
)


async def describe_personality(
    member_name: str,
    messages: List[str],
    brevity: str = "",
    image: Optional[str] = None,
) -> str:
    """Строит описание личности участника группы по его сообщениям (экспорту)
    и, если дано, по фото профиля (data URL, мультимодальной моделью).

    brevity — опциональная инструкция о длине ответа; если задана, портрет
    строится соответствующего объёма.
    """
    if not messages and not image:
        return "По этому участнику пока недостаточно сообщений, чтобы составить описание."
    convo_text = "\n".join(f"- {m}" for m in messages)
    user_prompt = (
        f"Участник: {member_name}\n\n"
        f"Его сообщения из группового чата:\n{convo_text}\n\n"
        f"Опиши его личность, интересы и привычки. Если дана фотография профиля — "
        f"опиши и её: внешность, настроение, что можно сказать по аватарке, "
        f"и как она дополняет портрет. Не выдумывай, чего нет на фото."
    )
    if brevity:
        user_prompt += (
            f"\n\nПользователь просит ответить {brevity}. Обязательно соблюди "
            f"указанную длину ответа — ни одного лишнего слова, только сама "
            f"характеристика."
        )
    try:
        model = config.GEMINI_VISION_MODEL if image else config.GEMINI_MODEL
        answer = await gemini_simple_text(
            PERSONALITY_SYSTEM_PROMPT, user_prompt, model=model,
            timeout=30, images=[image] if image else None,
        )
        return answer.strip() if answer else "Не удалось составить описание."
    except Exception as e:
        logger.error("Ошибка описания личности участника группы: %s", e)
        return "⚠️ Не удалось составить описание личности."


# ---------------------------------------------------------------------------
# Диспетчер (обратная совместимость): Gemini.
# ---------------------------------------------------------------------------
async def ask_llm_with_search(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
    images: Optional[List[str]] = None,
    chat_context: Optional[str] = None,
) -> str:
    from gemini_client import ask_gemini_with_search
    if images:
        gem_model = config.GEMINI_VISION_MODEL or config.GEMINI_MODEL
        return await ask_gemini_with_search(
            system_prompt, user_text, history=history,
            model=gem_model, use_thinking=False, images=images,
            chat_context=chat_context,
        )
    # Для Gemini игнорируем модель DeepSeek из настроек пользователя.
    return await ask_gemini_with_search(
        system_prompt, user_text, history=history,
        model=None, use_thinking=use_thinking, images=None,
        chat_context=chat_context,
    )
