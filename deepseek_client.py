import asyncio
import json
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote

from openai import AsyncOpenAI

import config
from model_manager import model_manager
from user_settings import MODEL_MAP

_VISION_MODEL = MODEL_MAP["vision"]

logger = logging.getLogger(__name__)

client = AsyncOpenAI(
    api_key=config.DEEPSEEK_API_KEY,
    base_url=config.DEEPSEEK_BASE_URL,
)

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
    "«Кто прилетел в Москву на военном самолёте?» → SEARCH: военные самолёты прилет Москва сегодня\n"
    "«Напиши стих» → NOSEARCH\n"
    "«Что такое контейнер?» → NOSEARCH\n"
    "«Объясни async/await» → NOSEARCH\n"
    "Сомневаешься? → ищи. Лучше поискать лишний раз, чем дать неполный ответ."
)


async def _ai_decides_search(
    user_text: str,
    model: Optional[str] = None,
) -> Optional[str]:
    effective_model = model or model_manager.get()
    try:
        response = await client.chat.completions.create(
            model=effective_model,
            messages=[
                {"role": "system", "content": SEARCH_DECISION_PROMPT},
                {"role": "user", "content": user_text},
            ],
            timeout=15,
        )
        answer = (response.choices[0].message.content or "").strip()
        if answer.upper().startswith("SEARCH:"):
            query = answer[len("SEARCH:"):].strip()
            if query:
                logger.info("AI решил искать: '%s' (для: %s)", query, user_text[:80])
                return query
        logger.info("AI решил не искать: %s", user_text[:80])
        return None
    except Exception as e:
        logger.error("Ошибка при решении о поиске: %s", e)
        return None


_IMG_URL_MARKERS = ("QRIMG=", "AVATARIMG=", "PICIMG=")


def _extract_image_urls_from_results(results: list) -> List[str]:
    """Вытащить URL сгенерированных картинок из текстов результатов инструментов."""
    urls: List[str] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        text = str(r)
        for marker in _IMG_URL_MARKERS:
            start = 0
            while True:
                idx = text.find(marker, start)
                if idx == -1:
                    break
                url = text[idx + len(marker):].split(" ")[0].split("\n")[0].strip()
                if url.startswith("http") and url not in urls:
                    urls.append(url)
                start = idx + len(marker)
    return urls


def _attach_tool_images(answer: str, image_urls: List[str]) -> str:
    if not image_urls:
        return answer
    missing = [u for u in image_urls if u not in answer]
    if not missing:
        return answer
    block = "\n\n" + "\n".join(f"[Картинка]({u})" for u in missing)
    return answer.strip() + block


async def ask_deepseek(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
    images: Optional[List[str]] = None,
) -> str:
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)

    # Мультимодальность: если переданы изображения (base64 data URL), формируем
    # user-сообщение как список content-блоков [text, image_url, ...].
    if images:
        content: List[Dict] = [{"type": "text", "text": user_text}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": img}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_text})

    effective_model = model or model_manager.get()

    kwargs = {
        "model": effective_model,
        "messages": messages,
        "timeout": 60,
    }
    # Пользовательские параметры генерации (temperature, top_p, max_tokens…).
    from gen_params import base_kwargs
    kwargs.update(base_kwargs())
    # При мультимодальных запросах (картинки) не добавляем thinking extra_body —
    # vision-модели/прокси часто его не принимают вместе с image_url.
    if use_thinking and not images:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

    # Транзиентные сбои (таймаут/сеть/перегрузка) ретраим с небольшим бэкоффом,
    # чтобы редкие глюки API не выбивали в фолбэк-сообщение.
    import openai as _openai
    _RETRYABLE = (
        _openai.APITimeoutError,
        _openai.APIConnectionError,
        _openai.RateLimitError,
    )

    # Агентный слой: передаём модели список API-инструментов, чтобы она сама
    # решала, когда ей не хватает данных (время, погода, курсы, поиск и т.п.),
    # вызывала нужный инструмент и продолжала, пока не будет готова ответить.
    # При мультимодальных (vision) запросах инструменты не подключаем: эти
    # модели/прокси часто не принимают tools вместе с image_url.
    from tools import as_tools_schema, run_tool
    if not images:
        kwargs["tools"] = as_tools_schema()

    async def _send() -> list:
        """Одна отправка к API с ретраями. Возвращает message объект."""
        last_exc = None
        for attempt in range(3):
            try:
                resp = await client.chat.completions.create(**kwargs)
                return resp.choices[0].message
            except _RETRYABLE as e:
                last_exc = e
                if attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
            except Exception as e:
                logger.error("Ошибка запроса к DeepSeek API (не ретраим): %s", e)
                raise
        logger.error("Ошибка запроса к DeepSeek API после ретраев: %s", last_exc)
        return None

    MAX_TOOL_ROUNDS = 4
    collected_images: List[str] = []
    for _round in range(MAX_TOOL_ROUNDS):
        msg = await _send()
        if msg is None:
            return _attach_tool_images(FALLBACK_ANSWER, collected_images)

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            answer = msg.content
            if answer and answer.strip():
                return _attach_tool_images(answer.strip(), collected_images)
            return _attach_tool_images(FALLBACK_ANSWER, collected_images)

        # Исполняем все вызванные инструменты параллельно.
        calls = [
            (tc.function.name, json.loads(tc.function.arguments or "{}"))
            for tc in tool_calls
        ]
        logger.info("TOOL_CALLS (%s): %s", len(calls), [n for n, _ in calls])
        results = await asyncio.gather(
            *(run_tool(name, args or {}) for name, args in calls),
            return_exceptions=True,
        )
        collected_images.extend(_extract_image_urls_from_results(results))

        kwargs["messages"].append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "type": "function",
                    "id": tc.id,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tool_calls
            ],
        })
        for tc, result in zip(tool_calls, results):
            if isinstance(result, Exception):
                result = f"Инструмент упал: {result}"
            kwargs["messages"].append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })

    # Превышен лимит итераций инструментов — вернуть последний текст, если есть.
    return _attach_tool_images(msg.content and msg.content.strip() or FALLBACK_ANSWER, collected_images)


async def ask_deepseek_with_search(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
    images: Optional[List[str]] = None,
    chat_context: Optional[str] = None,
) -> str:
    # Если есть контекст из истории чата — берём его, веб-поиск не запускаем.
    search_context = ""
    if chat_context:
        search_context = chat_context
    elif not images:
        search_query = await _ai_decides_search(user_text, model=model)
        if search_query:
            search_context = await web_search(search_query)

    if search_context:
        user_text = (
            user_text
            + "\n\n[Контекст из истории/поиска. ОБЯЗАТЕЛЬНО используй эти данные "
            "для ответа, перескажи своими словами со ссылками на источники]:\n"
            + search_context
        )

    return await ask_deepseek(
        system_prompt, user_text, history=history,
        model=model, use_thinking=use_thinking, images=images,
    )


async def summarize_profile(old_profile: str, recent_messages: List[Dict[str, str]]) -> str:
    convo_text = "\n".join(f"{m['role']}: {m['content']}" for m in recent_messages)
    user_prompt = (
        f"Текущая заметка о пользователе:\n{old_profile or '(пусто)'}\n\n"
        f"Новые сообщения из переписки:\n{convo_text}\n\n"
        f"Обнови заметку."
    )
    try:
        response = await client.chat.completions.create(
            model=model_manager.get(),
            messages=[
                {"role": "system", "content": PROFILE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            timeout=30,
        )
        answer = response.choices[0].message.content
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
        response = await client.chat.completions.create(
            model=model_manager.get(),
            messages=[
                {"role": "system", "content": MEMBER_PROFILE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            timeout=30,
        )
        answer = response.choices[0].message.content
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
    и, если дано, по фото профиля (data URL, vision-моделью).

    brevity — опциональная инструкция о длине ответа (например "одним словом"
    или "кратко"); если задана, портрет строится соответствующего объёма.
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
        kwargs = {
            "model": model_manager.get(),
            "messages": [
                {"role": "system", "content": PERSONALITY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "timeout": 30,
        }
        if image:
            kwargs["model"] = _VISION_MODEL
            kwargs["messages"][1]["content"] = [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        response = await client.chat.completions.create(**kwargs)
        answer = response.choices[0].message.content
        return answer.strip() if answer else "Не удалось составить описание."
    except Exception as e:
        logger.error("Ошибка описания личности участника группы: %s", e)
        return "⚠️ Не удалось составить описание личности."
