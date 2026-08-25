import asyncio
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote

from openai import AsyncOpenAI

import config
from model_manager import model_manager

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


async def ask_deepseek(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
) -> str:
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    effective_model = model or model_manager.get()

    try:
        kwargs = {
            "model": effective_model,
            "messages": messages,
            "timeout": 60,
        }
        if use_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        response = await client.chat.completions.create(**kwargs)
        answer = response.choices[0].message.content
        return answer.strip() if answer else FALLBACK_ANSWER
    except Exception as e:
        logger.error("Ошибка запроса к DeepSeek API: %s", e)
        return FALLBACK_ANSWER


async def ask_deepseek_with_search(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
) -> str:
    search_query = await _ai_decides_search(user_text, model=model)

    search_context = ""
    if search_query:
        search_context = await web_search(search_query)

    if search_context:
        user_text = (
            user_text
            + "\n\n[Результаты веб-поиска по этому запросу. "
            "ОБЯЗАТЕЛЬНО используй эти данные для ответа, "
            "перескажи своими словами со ссылками на источники]:\n"
            + search_context
        )

    return await ask_deepseek(
        system_prompt, user_text, history=history,
        model=model, use_thinking=use_thinking,
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
