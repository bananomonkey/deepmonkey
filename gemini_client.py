import asyncio
import logging
from typing import Dict, List, Optional

import httpx

import config

logger = logging.getLogger(__name__)

FALLBACK_ANSWER = "⚠️ Произошла ошибка при обращении к нейросети. Попробуйте позже."

_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _to_gemini_messages(
    messages: List[Dict[str, str]],
) -> List[Dict]:
    """Конвертирует [{role, content}] в формат Gemini 'contents'.

    Gemini не имеет понятия system-роли внутри contents — системный промт
    передаётся отдельным параметром 'systemInstruction'. Здесь мы фильтруем
    system-сообщения и нормализуем роли (assistant/tool -> model).
    """
    contents: List[Dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            continue
        if role == "model" or role == "assistant":
            g_role = "model"
        elif role == "tool":
            # Результат инструмента возвращается как обычный текст от модели-вызова.
            g_role = "model"
        else:
            g_role = "user"
        contents.append({"role": g_role, "parts": [{"text": content}]})
    return contents


def _data_url_to_inline(data_url: str) -> Dict:
    """Превращает data URL (data:image/...;base64,...) в {mimeType, data}."""
    if data_url.startswith("data:"):
        header, _, b64 = data_url.partition(",")
        mime = header[5:].split(";")[0] or "image/jpeg"
        return {"mimeType": mime, "data": b64}
    return {"mimeType": "image/jpeg", "data": data_url}


async def ask_gemini(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
    images: Optional[List[str]] = None,
) -> str:
    """Единый агентный вызов Gemini с поддержкой инструментов (tools) и ретраев."""
    from tools import as_gemini_tool_schema, run_tool

    effective_model = model or config.GEMINI_MODEL

    MAX_TOOL_ROUNDS = 4
    collected_images: List[str] = []

    async def _send(contents) -> dict | None:
        # Один вызов с ретраями на транзиентные сбои и fallback на второй ключ.
        keys = [k for k in (config.GEMINI_API_KEY, config.GEMINI_API_KEY_2) if k]
        if not keys:
            logger.error("Нет API-ключей Gemini")
            return None
        payload: Dict = {"contents": contents, "generationConfig": {}}
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        tools = as_gemini_tool_schema() if not images else None
        if tools:
            payload["tools"] = [{"functionDeclarations": tools}]
        for key in keys:
            url = _BASE + "/" + f"{effective_model}:generateContent?key={key}"
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=60) as client:
                        resp = await client.post(url, json=payload)
                        resp.raise_for_status()
                        return resp.json()
                except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as e:
                    if attempt < 2:
                        await asyncio.sleep(0.8 * (attempt + 1))
                        continue
                    logger.warning("Gemini API (ключ %s...) ошибка: %s", key[-4:], e)
        logger.error("Gemini API ошибка на всех ключах/ретраях")
        return None

    # Поезд сообщений: история + вопрос пользователя.
    contents: List[Dict] = _to_gemini_messages(list(history or []))
    if images:
        parts: List[Dict] = [{"text": user_text}]
        for img in images:
            parts.append({"inline_data": _data_url_to_inline(img)})
        contents.append({"role": "user", "parts": parts})
    else:
        contents.append({"role": "user", "parts": [{"text": user_text}]})

    answer_text = ""
    for _round_i in range(MAX_TOOL_ROUNDS):
        data = await _send(contents)
        if data is None:
            return _attach_tool_images(FALLBACK_ANSWER, collected_images)

        # Собираем functionCalls и текст.
        function_calls = []
        answer_text = ""
        candidates = data.get("candidates") or []
        for cand in candidates:
            parts = (cand.get("content") or {}).get("parts") or []
            for p in parts:
                if "text" in p:
                    answer_text += p.get("text", "")
                if "functionCall" in p:
                    fc = p["functionCall"]
                    function_calls.append({"name": fc.get("name", ""), "args": fc.get("args") or {}})

        if not function_calls:
            answer_text = answer_text.strip()
            if answer_text:
                return _attach_tool_images(answer_text, collected_images)
            return _attach_tool_images(FALLBACK_ANSWER, collected_images)

        # Добавляем вызовы функций как 'model'-сообщение.
        contents.append({
            "role": "model",
            "parts": [
                {"functionCall": {"name": f["name"], "args": f["args"]}}
                for f in function_calls
            ],
        })

        # Исполняем инструменты параллельно.
        logger.info("GEMINI TOOL_CALLS (%s): %s", len(function_calls), [f["name"] for f in function_calls])
        results = await asyncio.gather(
            *(run_tool(f["name"], f["args"] or {}) for f in function_calls),
            return_exceptions=True,
        )
        collected_images.extend(_extract_image_urls_from_results(results))

        # Результаты инструментов как 'functionResponse' (user-сообщение).
        resp_parts = []
        for fc, result in zip(function_calls, results):
            if isinstance(result, Exception):
                result = f"Инструмент упал: {result}"
            resp_parts.append(
                {
                    "functionResponse": {
                        "name": fc["name"],
                        "response": {"result": str(result)},
                    }
                }
            )
        contents.append({"role": "user", "parts": resp_parts})

    return _attach_tool_images(answer_text.strip() or FALLBACK_ANSWER, collected_images)


async def ask_gemini_with_search(
    system_prompt: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    use_thinking: bool = False,
    images: Optional[List[str]] = None,
    chat_context: Optional[str] = None,
) -> str:
    # Web-поиск подключаем через общий модуль deepseek_client (там уже реализованы
    # и DuckDuckGo, и Википедия, и Tavily).
    from deepseek_client import _ai_decides_search, web_search

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

    return await ask_gemini(
        system_prompt, user_text, history=history,
        model=model, use_thinking=use_thinking, images=images,
    )


_IMG_URL_MARKERS = ("QRIMG=", "AVATARIMG=", "PICIMG=")


def _extract_image_urls_from_results(results: list) -> List[str]:
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
