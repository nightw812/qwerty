"""
Утилиты для работы с эмодзи.
"""

import config

_EMOJI_MAP = {
    "start": "▶️",
    "stop": "⏹",
    "clock": "🕐",
    "stats": "📊",
    "account": "👤",
    "groups": "👥",
    "content": "📝",
    "settings": "⚙️",
    "schedule": "🗓",
    "faq": "❓",
    "back": "◀️",
    "cancel": "❌",
    "add": "➕",
    "delete": "🗑",
    "check": "✅",
    "cross": "❌",
    "circle": "⚪",
}


def plain(key: str) -> str:
    """Возвращает обычный эмодзи по ключу."""
    return _EMOJI_MAP.get(key, "")


def emoji_id(key: str) -> str | None:
    """Возвращает ID кастомного эмодзи Telegram Premium, если задан в config."""
    return config.CUSTOM_EMOJI_IDS.get(key)


def emoji(key: str) -> str:
    """Возвращает обычный эмодзи (для совместимости)."""
    return plain(key)
