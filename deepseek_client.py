import logging
from typing import Dict, List, Optional

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


async def web_search(query: str, max_results: int = 5) -> str:
    ddgs = _get_search_client()
    if ddgs is None:
        return ""
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None, lambda: list(ddgs.text(query, max_results=max_results))
        )
        if not results:
            return ""
        parts = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            parts.append(f"{i}. {title}\n{body}\n{href}")
        return "\n\n".join(parts)
    except Exception as e:
        logger.error("Ошибка DuckDuckGo поиска: %s", e)
        return ""


SEARCH_DECISION_PROMPT = (
    "Тебе дан запрос пользователя. Ты решаешь, нужны ли тебе актуальные данные "
    "из интернета для качественного ответа. Если да — ответь СТРОКОЙ:\n"
    "SEARCH: <поисковый запрос на английском или русском, оптимальный для поисковика>\n"
    "Если нет — ответь только:\n"
    "NOSEARCH\n"
    "Примеры:\n"
    "Запрос: «Кто президент Франции?» → SEARCH: president of France 2026\n"
    "Запрос: «Что такое Python?» → NOSEARCH\n"
    "Запрос: «Курс доллара сегодня» → SEARCH: USD to RUB exchange rate today\n"
    "Запрос: «Напиши стих» → NOSEARCH\n"
    "Запрос: «Какие новости в ИТ сегодня?» → SEARCH: IT news today 2026\n"
    "Не объясняй свой выбор. Только SEARCH: или NOSEARCH."
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
                logger.info("AI решил искать: '%s' (для запроса: %s)", query, user_text[:80])
                return query
        logger.info("AI решил не искать для: %s", user_text[:80])
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

    augmented_prompt = system_prompt
    if search_context:
        augmented_prompt += (
            "\n\n[Актуальные данные из веб-поиска — используй если релевантно, "
            "не зачитывай источник дословно]:\n" + search_context
        )

    return await ask_deepseek(
        augmented_prompt, user_text, history=history,
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
