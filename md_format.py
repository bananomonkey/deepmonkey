import html
import re

# Блоки кода ```lang\n...``` — обрабатываем ДО остального форматирования
_CODE_BLOCK_RE = re.compile(r"```(\w+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*)$", re.MULTILINE)
_LIST_MARKER_RE = re.compile(r"^(\s*)[-*][ \t]+", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)|(?<!_)_(?!_)([^_\n]+?)_(?!_)")


def markdown_to_html(text: str) -> str:
    """
    Конвертирует упрощённый Markdown (в таком виде обычно отвечает DeepSeek:
    **bold**, `code`, ```блоки кода```, [ссылки](url), заголовки #, списки -/*)
    в HTML, понятный Telegram (parse_mode="HTML") — чтобы это реально
    отображалось форматированием, а не звёздочками и решётками в тексте.

    Не претендует на 100% поддержку всего синтаксиса Markdown — покрывает
    то, что реально используют языковые модели в ответах.
    """
    if not text:
        return text

    # 1. Экранируем HTML-спецсимволы из исходного текста ИИ,
    #    чтобы случайные < > & не сломали разметку и не считались тегами.
    text = html.escape(text, quote=False)

    # 2. Блоки кода — прячем содержимое от дальнейших замен через плейсхолдеры,
    #    чтобы **, _ и т.п. внутри кода не превращались в теги форматирования.
    code_blocks = []

    def _stash_code_block(m: "re.Match[str]") -> str:
        lang = m.group(1) or ""
        code = m.group(2)
        cls = f' class="language-{lang}"' if lang else ""
        code_blocks.append(f"<pre><code{cls}>{code}</code></pre>")
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(_stash_code_block, text)

    inline_codes = []

    def _stash_inline_code(m: "re.Match[str]") -> str:
        inline_codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_stash_inline_code, text)

    # 3. Маркеры списков "- "/"* " -> "• " — делаем ДО обработки жирного/курсива,
    #    чтобы одиночные "*" в начале строки не путались с *italic*.
    text = _LIST_MARKER_RE.sub(lambda m: f"{m.group(1)}• ", text)

    # 4. Заголовки -> жирный текст (Telegram не поддерживает <h1> и т.п.)
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(1).strip()}</b>", text)

    # 5. Ссылки [текст](url)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)

    # 6. Жирный текст (после списков/заголовков, до курсива)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)

    # 7. Курсив
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)

    # 8. Возвращаем на место инлайн-код и блоки кода
    for i, code_html in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code_html)
    for i, block_html in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", block_html)

    return text
