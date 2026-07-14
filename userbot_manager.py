"""
Управляет Telethon-клиентами. У каждого пользователя бота может быть
НЕСКОЛЬКО СОБСТВЕННЫХ аккаунтов — каждый идентифицируется session_key
вида "<user_id>_<account_index>". Каждый аккаунт пользователь подключает
сам (свой номер, свой код).
"""

import os
import asyncio
import random

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

import config
import spintax

_clients: dict[str, TelegramClient] = {}


def _session_path(session_key: str) -> str:
    os.makedirs(config.SESSIONS_DIR, exist_ok=True)
    return os.path.join(config.SESSIONS_DIR, f"user_{session_key}")


async def get_client(session_key: str) -> TelegramClient:
    client = _clients.get(session_key)
    if client is None:
        client = TelegramClient(_session_path(session_key), config.API_ID, config.API_HASH)
        _clients[session_key] = client
    if not client.is_connected():
        await client.connect()
    return client


async def is_authorized(session_key: str) -> bool:
    client = await get_client(session_key)
    return await client.is_user_authorized()


async def request_code(session_key: str, phone: str):
    """Отправляет код подтверждения на указанный номер. Возвращает phone_code_hash."""
    client = await get_client(session_key)
    sent = await client.send_code_request(phone)
    return sent.phone_code_hash


async def sign_in_code(session_key: str, phone: str, code: str, phone_code_hash: str):
    """Пробует войти по коду. Бросает SessionPasswordNeededError, если включена 2FA."""
    client = await get_client(session_key)
    await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)


async def sign_in_password(session_key: str, password: str):
    client = await get_client(session_key)
    await client.sign_in(password=password)


async def discard_client(session_key: str):
    """
    Полностью забывает клиента — используется, когда вход отменён или прерван
    (например, отмена на этапе пароля 2FA). Без этого повторная попытка входа
    на тот же account_index переиспользует того же "подвешенного" клиента и
    падает со странной ошибкой вида "Two-steps verification is enabled...".
    Log_out не вызываем — валидной авторизации там ещё нет, вызов log_out
    на невалидной/недологиненной сессии сам может упасть с ошибкой.
    """
    client = _clients.pop(session_key, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    path = _session_path(session_key) + ".session"
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


async def logout(session_key: str):
    client = await get_client(session_key)
    try:
        await client.log_out()
    finally:
        _clients.pop(session_key, None)


def _can_post(entity) -> bool:
    """
    Определяет, может ли аккаунт писать в этот чат — используя поля, уже
    присутствующие в объекте диалога (без дополнительных запросов к Telegram,
    которые были и медленными, и ошибались на несуществующем атрибуте).
    """
    if isinstance(entity, Chat):
        if getattr(entity, "left", False) or getattr(entity, "deactivated", False):
            return False
        if getattr(entity, "creator", False) or getattr(entity, "admin_rights", None):
            return True
        banned = getattr(entity, "default_banned_rights", None)
        if banned and getattr(banned, "send_messages", False):
            return False
        return True

    if isinstance(entity, Channel):
        if getattr(entity, "left", False):
            return False
        if getattr(entity, "creator", False):
            return True
        admin_rights = getattr(entity, "admin_rights", None)
        if admin_rights and getattr(admin_rights, "post_messages", False):
            return True
        if getattr(entity, "broadcast", False):
            # Обычный (не админ) участник канала писать не может.
            return False
        # Супергруппа: смотрим персональные и общие для чата ограничения.
        personal_banned = getattr(entity, "banned_rights", None)
        if personal_banned and getattr(personal_banned, "send_messages", False):
            return False
        default_banned = getattr(entity, "default_banned_rights", None)
        if default_banned and getattr(default_banned, "send_messages", False):
            return False
        return True

    return True


async def fetch_groups(session_key: str) -> list[dict]:
    """
    Возвращает список групп/каналов, где состоит данный аккаунт И где у него
    есть право отправлять сообщения (каналы/группы без права постить пропускаются).
    """
    client = await get_client(session_key)
    groups = []
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        if not _can_post(dialog.entity):
            continue
        groups.append({"id": dialog.id, "name": dialog.name})
    return groups


async def broadcast(session_key: str, chat_ids: list[int], content: dict, delay_spec: dict):
    """
    Рассылает контент только в группы из chat_ids (аккаунт должен уже там состоять).

    content = {
        "type": "text" | "photo" | "photo_text" | "forward",
        "text": str | None,                 # может содержать спинтакс {a|b} и HTML-разметку
        "photo": путь к файлу изображения | None,
        "forward_chat_id": int | None,       # для "forward"
        "forward_message_id": int | None,    # для "forward"
    }
    Текст резолвится ЗАНОВО перед каждой отправкой (спинтакс), чтобы сообщения
    в разных группах отличались.

    delay_spec = {"min": float, "max": float} — пауза между отправками, секунды.
    Если min == max — пауза фиксированная, иначе каждая пауза выбирается случайно
    в этом диапазоне (с точностью до сотых секунды).
    """
    client = await get_client(session_key)
    content_type = content.get("type")
    text_template = content.get("text")
    photo = content.get("photo")
    fwd_chat = content.get("forward_chat_id")
    fwd_msg = content.get("forward_message_id")

    lo = delay_spec.get("min", 0)
    hi = delay_spec.get("max", lo)
    if hi < lo:
        lo, hi = hi, lo

    sent, failed = 0, 0
    for chat_id in chat_ids:
        try:
            if content_type == "text":
                text = spintax.resolve(text_template)
                await client.send_message(chat_id, text, parse_mode="html")
            elif content_type == "photo":
                await client.send_file(chat_id, photo)
            elif content_type == "photo_text":
                text = spintax.resolve(text_template)
                await client.send_file(chat_id, photo, caption=text, parse_mode="html")
            elif content_type == "forward":
                # Пересылка сообщения из канала/группы
                await client.forward_messages(chat_id, messages=fwd_msg, from_peer=fwd_chat)
            else:
                failed += 1
                continue
            sent += 1
        except Exception:
            failed += 1
        pause = round(random.uniform(lo, hi), 2) if hi > lo else lo
        if pause > 0:
            await asyncio.sleep(pause)
    return sent, failed
