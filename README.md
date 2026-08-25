# Telegram-бот на aiogram 3.x

Бот с обычным режимом чата (с памятью и несколькими чатами), inline-режимом
с анимацией ответа и цепочкой реплаев, антиспамом, персонализацией через
профиль пользователя и полноценной админ-панелью.

## Структура проекта

```
project/
├── handlers/
│   ├── __init__.py
│   ├── user.py         # /start, чаты, инлайн-режим, реплаи в группах
│   └── admin.py         # /admin: промт, модель, статистика, бан, рассылка, профиль
├── config.py             # чтение переменных окружения + guard-инструкция
├── prompt_manager.py      # хранение системного промта + сборка итогового
├── model_manager.py        # текущая модель DeepSeek (меняется через /admin)
├── user_storage.py          # база пользователей: профиль, бан, счётчики
├── chat_sessions.py          # несколько чатов на пользователя (личка)
├── reply_context_store.py     # контекст для inline-режима и реплаев в группах
├── rate_limiter.py             # антиспам (rate limit)
├── md_format.py                 # конвертер Markdown -> Telegram HTML
├── deepseek_client.py            # асинхронный клиент DeepSeek + профиль-саммари
├── filters.py                     # фильтр IsAdmin
├── keyboards.py                    # inline-клавиатуры
├── states.py                        # FSM-состояния
├── main.py                           # точка входа (polling)
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
