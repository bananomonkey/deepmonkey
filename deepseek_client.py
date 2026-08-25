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


async def ask_deepseek(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Отправляет запрос в DeepSeek с системным промтом, историей диалога (если
    есть) и новым сообщением пользователя. Модель берётся из model_manager —
    её можно менять через админ-панель без рестарта бота.
    """
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        response = await client.chat.completions.create(
            model=model_manager.get(),
            messages=messages,
            timeout=60,
        )
        answer = response.choices[0].message.content
        return answer.strip() if answer else FALLBACK_ANSWER
    except Exception as e:
        logger.error("Ошибка запроса к DeepSeek API: %s", e)
        return FALLBACK_ANSWER


async def summarize_profile(old_profile: str, recent_messages: List[Dict[str, str]]) -> str:
    """
    Обновляет краткую заметку о пользователе (интересы/факты) на основе
    последних сообщений — используется и для персонализации ответов ИИ,
    и для просмотра админом через "Профиль пользователя".
    """
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
