"""
Управляет Telethon-клиентами. У каждого пользователя бота может быть
НЕСКОЛЬКО СОБСТВЕННЫХ аккаунтов — каждый идентифицируется session_key
вида "<user_id>_<account_index>". Каждый аккаунт пользователь подключает
сам (свой номер, свой код).
"""

import os
import asyncio
import random
import logging

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
)

import config
import spintext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    есть право отправлять сообщения. Максимум config.MAX_GROUPS_PER_ACCOUNT.
    """
    client = await get_client(session_key)
    groups = []
    try:
        async for dialog in client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue
            if not _can_post(dialog.entity):
                continue
            groups.append({"id": dialog.id, "name": dialog.name})
            # Ограничиваем количество групп
            if len(groups) >= config.MAX_GROUPS_PER_ACCOUNT:
                break
    except FloodWaitError as e:
        logger.warning(f"FloodWait при загрузке групп: {e.seconds} сек.")
        await asyncio.sleep(e.seconds)
        # Повторяем попытку
        return await fetch_groups(session_key)
    except Exception as e:
        logger.error(f"Ошибка загрузки групп: {e}")
    return groups


async def broadcast(session_key: str, chat_ids: list[int], content: dict, delay_spec: dict):
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
                text = spintext.resolve(text_template) if text_template else ""
                await client.send_message(chat_id, text, parse_mode="html")

            elif content_type == "photo":
                await client.send_file(chat_id, photo)

            elif content_type == "photo_text":
                text = spintext.resolve(text_template) if text_template else ""
                await client.send_file(chat_id, photo, caption=text, parse_mode="html")

            elif content_type == "forward":
                # Пытаемся переслать
                try:
                    await client.forward_messages(chat_id, fwd_msg, from_peer=fwd_chat)
                except Exception as e:
                    logger.error(f"Ошибка forward_messages: {e}. Пробуем скопировать содержимое.")
                    # Fallback: если не удалось переслать, пытаемся получить исходное сообщение и отправить как новое
                    # Для этого нужно получить сообщение по fwd_chat и fwd_msg
                    try:
                        original_msg = await client.get_messages(fwd_chat, ids=fwd_msg)
                        if original_msg:
                            if original_msg.text:
                                await client.send_message(chat_id, original_msg.text, parse_mode="html")
                            elif original_msg.photo:
                                # Скачиваем фото и отправляем
                                file_path = await client.download_media(original_msg.photo)
                                if file_path:
                                    await client.send_file(chat_id, file_path, caption=original_msg.caption)
                                    # удаляем временный файл
                                    try:
                                        os.remove(file_path)
                                    except:
                                        pass
                            else:
                                # Другие типы медиа можно добавить
                                logger.warning(f"Не поддерживаемый тип медиа в сообщении {fwd_msg}")
                                failed += 1
                                continue
                        else:
                            logger.error(f"Не удалось получить сообщение {fwd_msg} из {fwd_chat}")
                            failed += 1
                            continue
                    except Exception as e2:
                        logger.error(f"Ошибка при получении сообщения: {e2}")
                        failed += 1
                        continue

            else:
                logger.warning(f"Неизвестный тип контента: {content_type}")
                failed += 1
                continue

            sent += 1
            logger.info(f"Отправлено в {chat_id} (акк {session_key})")

        except FloodWaitError as e:
            logger.warning(f"FloodWait на {chat_id}: {e.seconds} сек.")
            await asyncio.sleep(e.seconds)
            # Пробуем повторно отправить (упрощённо)
            try:
                # Повторяем ту же логику с повторной попыткой (можно вынести в функцию)
                if content_type == "forward":
                    try:
                        await client.forward_messages(chat_id, fwd_msg, from_peer=fwd_chat)
                    except Exception as e:
                        # fallback
                        original_msg = await client.get_messages(fwd_chat, ids=fwd_msg)
                        if original_msg:
                            if original_msg.text:
                                await client.send_message(chat_id, original_msg.text, parse_mode="html")
                            elif original_msg.photo:
                                file_path = await client.download_media(original_msg.photo)
                                if file_path:
                                    await client.send_file(chat_id, file_path, caption=original_msg.caption)
                                    try:
                                        os.remove(file_path)
                                    except:
                                        pass
                # ... аналогично для других типов
                sent += 1
            except Exception as e2:
                logger.error(f"Ошибка повторной отправки: {e2}")
                failed += 1

        except Exception as e:
            logger.error(f"Ошибка отправки в {chat_id}: {e}")
            failed += 1

        # Пауза между отправками
        pause = round(random.uniform(lo, hi), 2) if hi > lo else lo
        if pause > 0:
            await asyncio.sleep(pause)

    logger.info(f"Рассылка завершена: {sent} отправлено, {failed} ошибок")
    return sent, failed

def _cleanup_temp_photo(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
