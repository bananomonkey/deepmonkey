import asyncio
import time
from collections import defaultdict, deque
from typing import Deque, Dict

import config


class RateLimiter:
    """
    Простой rate limiter по скользящему окну в памяти: не более
    `max_requests` запросов от одного user_id за `window_seconds`.

    Защищает токены DeepSeek от выжигания спамом и грубо ограничивает
    возможность заDDoS'ить бота потоком сообщений/инлайн-запросов —
    без этого один человек может за секунду отправить десятки сообщений
    и все они уйдут в платный API.

    Хранится только в памяти (не персистится) — это осознанно: это защита
    от "здесь и сейчас" всплеска, а не долгосрочный бан (для этого есть
    отдельный персистентный бан в user_storage).
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: Dict[int, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            q = self._hits[user_id]
            while q and now - q[0] > self.window_seconds:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True


rate_limiter = RateLimiter(config.RATE_LIMIT_MESSAGES, config.RATE_LIMIT_WINDOW_SECONDS)
