"""
Управляет Telethon-клиентами. У каждого пользователя бота может быть
НЕСКОЛЬКО СОБСТВЕННЫХ аккаунтов — каждый идентифицируется session_key
вида "<user_id>_<account_index>". Каждый аккаунт пользователь подключает
сам (свой номер, свой код).
"""

import os
import asyncio
import glob
import random

from telethon import TelegramClient

import config

_clients: dict[str, TelegramClient] = {}


def _session_path(session_key: str) -> str:
    os.makedirs(config.SESSIONS_DIR, exist_ok=True)
    return os.path.join(config.SESSIONS_DIR, f"user_{session_key}")


def list_local_session_indices(user_id: int) -> list[int]:
    """Ищет файлы сессий вида user_<user_id>_<index>.session на диске
    (созданные локальным скриптом add_account_cli.py)."""
    pattern = os.path.join(config.SESSIONS_DIR, f"user_{user_id}_*.session")
    indices = []
    for path in glob.glob(pattern):
        name = os.path.basename(path)[: -len(".session")]  # user_<id>_<idx>
        try:
            idx = int(name.rsplit("_", 1)[-1])
            indices.append(idx)
        except ValueError:
            continue
    return sorted(indices)


async def get_account_phone(session_key: str) -> str | None:
    client = await get_client(session_key)
    if not await client.is_user_authorized():
        return None
    me = await client.get_me()
    return f"+{me.phone}" if me.phone else None


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


async def logout(session_key: str):
    client = await get_client(session_key)
    try:
        await client.log_out()
    finally:
        _clients.pop(session_key, None)


async def fetch_groups(session_key: str) -> list[dict]:
    """Возвращает список групп/каналов, где состоит данный аккаунт."""
    client = await get_client(session_key)
    groups = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            groups.append({"id": dialog.id, "name": dialog.name})
    return groups


async def broadcast(session_key: str, chat_ids: list[int], content: dict, delay_spec: dict):
    """
    Рассылает контент только в группы из chat_ids (аккаунт должен уже там состоять).

    content = {
        "type": "text" | "photo" | "photo_text",
        "text": str | None,
        "photo": путь к файлу изображения | None,
    }
    delay_spec = {"min": float, "max": float} — пауза между отправками, секунды.
    Если min == max — пауза фиксированная, иначе каждая пауза выбирается случайно
    в этом диапазоне (с точностью до сотых секунды).
    """
    client = await get_client(session_key)
    content_type = content.get("type")
    text = content.get("text")
    photo = content.get("photo")

    lo = delay_spec.get("min", 0)
    hi = delay_spec.get("max", lo)
    if hi < lo:
        lo, hi = hi, lo

    sent, failed = 0, 0
    for chat_id in chat_ids:
        try:
            if content_type == "text":
                await client.send_message(chat_id, text)
            elif content_type == "photo":
                await client.send_file(chat_id, photo)
            elif content_type == "photo_text":
                await client.send_file(chat_id, photo, caption=text)
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
